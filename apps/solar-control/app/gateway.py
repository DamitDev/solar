"""OpenAI-compatible API gateway with routing and load balancing.

All routing state is stored in Redis for multi-replica consistency.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import aiohttp

from app.config import settings
from app.database.hosts import host_db
from app.models import HostStatus, RegistryEntry
from app.redis_state import health_store, host_store, registry_store, routing_store
from app.services.model_access import filter_aliases_for_patterns

logger = logging.getLogger(__name__)


def _task_done_callback(task: asyncio.Task) -> None:
    """Log exceptions from fire-and-forget tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.error("Background task %s failed: %s", task.get_name(), exc)


_RETRYABLE_STATUSES = frozenset({502, 503, 504})

# How long to remember that a cache-aware backend answered usage without a
# cached split. Old llama.cpp/SGLang builds do not emit
# prompt_tokens_details; without this, every chat request pays an extra
# host round-trip that can never fill the gap.
_NEGATIVE_CACHE_TTL_S = 60

# How old a host last-generation record may be when only the cached split is
# missing. The strict 5s attribution window exists so a STALE generation's
# token COUNTS cannot be billed to a fresher request; the cached split is a
# cache-state property, not a per-request count, so a record up to two
# minutes old is still a fair fill. Verified against production 2026-08-17:
# the SGLang host record lags the 2s /metrics poll, so on a quiet instance
# the 5s fetch 404s and the row stays NULL with real data available.
_CACHED_FILL_WINDOW_S = 120

# Endpoints whose streaming form accepts ``stream_options.include_usage`` and
# answers with a terminal usage chunk. Both are relayed by stream_request, and
# without the option their token counts would fall back to the host's
# last-generation endpoint, which cannot attribute a request under concurrency.
_STREAM_USAGE_ENDPOINTS = frozenset({"/v1/chat/completions", "/v1/completions"})


class OpenAIGateway:

    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._bg_tasks: list[asyncio.Task[None]] = []
        self._pending_tasks: set[asyncio.Task] = set()
        self._stop_event: asyncio.Event | None = None
        # instance_key -> monotonic deadline; see _fill_usage_gaps.
        self._no_cached_until: dict[str, float] = {}

    async def _ensure_session(self) -> None:
        if self.session is not None and not self.session.closed:
            return
        async with self._session_lock:
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        for t in self._pending_tasks:
            t.cancel()
        self._pending_tasks.clear()

    def _spawn_task(self, coro, *, name: str | None = None) -> asyncio.Task:
        """Create a tracked fire-and-forget task with error logging."""
        task = asyncio.create_task(coro, name=name)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        task.add_done_callback(_task_done_callback)
        return task

    # ── Model registry ────────────────────────────────────────

    @staticmethod
    def _ws_cache_from_http_instances(
        instances: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Convert solar-host REST instance payloads to the WS cache shape."""
        cached: list[dict[str, Any]] = []
        for instance in instances:
            config = instance.get("config") or {}
            context_size = RegistryEntry._extract_context_size(instance)
            item = {
                "id": instance.get("id"),
                "alias": config.get("alias", instance.get("alias", "unknown")),
                "status": instance.get("status"),
                "port": instance.get("port"),
                "supported_endpoints": instance.get(
                    "supported_endpoints",
                    RegistryEntry.DEFAULT_ENDPOINTS,
                ),
                # Without it a cache re-seeded from HTTP would forward the alias
                # to a backend serving a translated name (SGLang and its colon).
                "served_model_name": instance.get("served_model_name"),
                "backend_type": config.get(
                    "backend_type",
                    instance.get("backend_type", "llamacpp"),
                ),
                "api_key": config.get("api_key"),
                # Ownership/identity fields the reconciler relies on —
                # without them a re-seeded cache entry looks unmanaged and
                # the reconciler re-creates duplicates (one-per-host
                # violated, surplus cleanup races in-flight starts).
                "managed_by": instance.get("managed_by"),
                "intent_id": instance.get("intent_id"),
                "model_source": config.get("model_source")
                or instance.get("model_source"),
                "priority": instance.get("priority"),
            }
            if context_size is not None:
                item["ctx_size"] = context_size
            cached.append(item)
        return cached

    async def refresh_model_registry(self) -> None:
        """Refresh the model registry from all hosts and store in Redis."""
        await self._ensure_session()
        if not self.session:
            return

        from app.socketio_app.host_handlers import (
            get_host_instances,
            is_host_connected,
        )

        new_model_map: dict[str, list[RegistryEntry]] = defaultdict(list)
        hosts = await host_db.get_all_hosts()
        # Tracked per host, not as one boolean: a single unreachable host must
        # not stop healthy hosts from de-registering aliases whose instances
        # genuinely went away (see the carry-forward below).
        failed_host_ids: set[str] = set()

        ws_hosts = []
        http_hosts = []
        missing_cache_hosts = []
        for host in hosts:
            if await is_host_connected(host.id):
                ws_hosts.append(host)
            else:
                http_hosts.append(host)

        for host in ws_hosts:
            ws_instances = await get_host_instances(host.id)
            await host_db.update_host_status(host.id, HostStatus.ONLINE)
            if not ws_instances:
                logger.warning(
                    "Host %s is connected but has no cached instances; polling HTTP",
                    host.id,
                )
                missing_cache_hosts.append(host)
                continue

            for instance in ws_instances:
                if instance.get("status") == "running":
                    entry = RegistryEntry.from_ws_instance(
                        host.id, host.url, host.api_key, instance
                    )
                    if entry:
                        new_model_map[entry.model_alias].append(entry)

        if http_hosts or missing_cache_hosts:
            now = time.time()
            grace = settings.disconnect_grace_period_s
            reconnect_interval = settings.reconnect_request_interval_s

            grace_hosts = []
            poll_hosts = list(missing_cache_hosts)

            for host in http_hosts:
                dc_ts = await host_store.get_disconnect_time(host.id)
                if dc_ts is not None and (now - dc_ts) < grace:
                    grace_hosts.append(host)
                else:
                    if dc_ts is not None:
                        last_req = await host_store.get_reconnect_request_time(host.id)
                        if last_req is None or (now - last_req) >= reconnect_interval:
                            self._spawn_task(
                                self._request_host_reconnect(host),
                                name=f"reconnect-{host.id[:8]}",
                            )
                    poll_hosts.append(host)

            for host in grace_hosts:
                cached = await get_host_instances(host.id)
                for instance in cached:
                    if instance.get("status") == "running":
                        entry = RegistryEntry.from_ws_instance(
                            host.id, host.url, host.api_key, instance
                        )
                        if entry:
                            new_model_map[entry.model_alias].append(entry)

            if poll_hosts:

                async def poll_host(host):
                    result_entries: list[RegistryEntry] = []
                    try:
                        url = f"{host.url}/instances"
                        headers = {"X-API-Key": host.api_key}
                        async with self.session.get(
                            url,
                            headers=headers,
                            timeout=aiohttp.ClientTimeout(total=5),
                        ) as response:
                            if response.status == 200:
                                instances = await response.json()
                                await host_store.set_host_instances(
                                    host.id,
                                    self._ws_cache_from_http_instances(instances),
                                )
                                prev_status = host.status
                                await host_db.update_host_status(
                                    host.id, HostStatus.ONLINE
                                )
                                if prev_status != HostStatus.ONLINE:
                                    await self._notify_host_online(host)
                                for instance in instances:
                                    if instance.get("status") == "running":
                                        entry = RegistryEntry.from_http_instance(
                                            host.id, host.url, instance
                                        )
                                        if entry:
                                            result_entries.append(entry)
                            else:
                                failed_host_ids.add(host.id)
                                await host_db.update_host_status(
                                    host.id, HostStatus.ERROR
                                )
                    except Exception:  # noqa: BLE001
                        failed_host_ids.add(host.id)
                        cached = await get_host_instances(host.id)
                        if cached:
                            for instance in cached:
                                if instance.get("status") == "running":
                                    entry = RegistryEntry.from_ws_instance(
                                        host.id, host.url, host.api_key, instance
                                    )
                                    if entry:
                                        result_entries.append(entry)
                        else:
                            await host_db.update_host_status(
                                host.id, HostStatus.OFFLINE
                            )
                    return result_entries

                results = await asyncio.gather(
                    *[poll_host(h) for h in poll_hosts], return_exceptions=True
                )
                for result in results:
                    if isinstance(result, Exception):
                        continue
                    for entry in result:
                        new_model_map[entry.model_alias].append(entry)

        if failed_host_ids:
            await self._carry_forward_failed_hosts(new_model_map, failed_host_ids)

        await registry_store.set_registry(dict(new_model_map))

    @staticmethod
    async def _carry_forward_failed_hosts(
        new_model_map: dict[str, list[RegistryEntry]],
        failed_host_ids: set[str],
    ) -> None:
        """Re-add previous registry entries owned by hosts we could not reach.

        Evidence from a host we did reach is authoritative for that host's own
        aliases, so an alias whose instances disappeared on a healthy host is
        allowed to de-register. Only entries belonging to ``failed_host_ids``
        are carried forward — the alternative (keeping the whole previous
        registry whenever any host failed) leaves ``/v1/models`` advertising
        models that no longer exist and routes requests at dead upstreams.
        """
        previous_registry = await registry_store.get_registry()
        if not previous_registry:
            return

        present = {
            (entry.host_id, entry.instance_id)
            for entries in new_model_map.values()
            for entry in entries
        }
        carried = 0
        for alias, entries in previous_registry.items():
            for entry in entries:
                if entry.host_id not in failed_host_ids:
                    continue
                key = (entry.host_id, entry.instance_id)
                if key in present:
                    continue
                present.add(key)
                new_model_map.setdefault(alias, []).append(entry)
                carried += 1

        if carried:
            logger.warning(
                "Carried forward %d registry entries from %d unreachable host(s); "
                "aliases on reachable hosts were refreshed normally",
                carried,
                len(failed_host_ids),
            )

    async def _notify_host_online(self, host) -> None:
        """Emit host_status to WebUI when HTTP polling discovers a host is online."""
        from app.services.host_status import build_host_status_payload
        from app.socketio_app.server import sio

        try:
            refreshed = await host_db.get_host(host.id)
            h = refreshed or host
            payload = await build_host_status_payload(h, connected=False)
            await sio.emit("host_status", payload.model_dump(), namespace="/webui")
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to notify WebUI of host online: %s", e)

    async def _request_host_reconnect(self, host) -> None:
        """Ask a disconnected host to re-establish its Socket.IO connection."""
        await self._ensure_session()
        if not self.session:
            return
        try:
            await host_store.set_reconnect_request_time(host.id)
            url = f"{host.url}/reconnect"
            headers = {"X-API-Key": host.api_key}
            async with self.session.post(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(
                        "Reconnect request to '%s' (%s): %s",
                        host.name,
                        host.id,
                        data.get("status"),
                    )
                else:
                    logger.debug(
                        "Reconnect request to '%s' failed: HTTP %s",
                        host.name,
                        resp.status,
                    )
        except Exception as e:  # noqa: BLE001
            logger.debug("Reconnect request to '%s' error: %s", host.name, e)

    # ── Background tasks ──────────────────────────────────────

    async def start_background_tasks(self) -> None:
        if self._stop_event is not None:
            return
        self._stop_event = asyncio.Event()
        await self.refresh_model_registry()
        self._bg_tasks = [
            asyncio.create_task(self._registry_refresh_loop(), name="registry_refresh"),
            asyncio.create_task(self._health_probe_loop(), name="health_probe"),
        ]

    async def stop_background_tasks(self) -> None:
        if self._stop_event is None:
            return
        self._stop_event.set()
        for t in self._bg_tasks:
            t.cancel()
        for t in self._bg_tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._bg_tasks = []
        self._stop_event = None

    async def _registry_refresh_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self.refresh_model_registry()
            except Exception:
                logger.exception("Registry refresh failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=settings.registry_refresh_interval_s,
                )
            except asyncio.TimeoutError:
                pass

    async def _health_probe_loop(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self._probe_all_instances_once()
            except Exception:
                logger.exception("Health probe failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=settings.health_check_interval_s,
                )
            except asyncio.TimeoutError:
                pass

    async def _probe_all_instances_once(self) -> None:
        registry = await registry_store.get_registry()
        instances: list[tuple[str, str, str]] = []
        for inst_list in registry.values():
            for inst in inst_list:
                instances.append((inst.host_id, inst.instance_id, inst.url))

        sem = asyncio.Semaphore(20)

        async def _probe_one(host_id: str, instance_id: str, url: str) -> None:
            async with sem:
                ok = await self._tcp_connect_ok(url)
                if ok:
                    await health_store.mark_healthy(
                        host_id, instance_id, ttl_s=settings.health_ttl_s + 2
                    )

        await asyncio.gather(
            *[_probe_one(h, i, u) for h, i, u in instances],
            return_exceptions=True,
        )

    async def _tcp_connect_ok(self, url: str) -> bool:
        try:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            hostname = parsed.hostname
            port = parsed.port
            if not hostname or not port:
                return False
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(hostname, port),
                timeout=settings.health_check_interval_s / 2,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:  # noqa: BLE001
            return False

    # ── Helpers ────────────────────────────────────────────────

    def _extract_usage_from_result(self, result: dict[str, Any]) -> dict[str, Any]:
        usage = result.get("usage") if isinstance(result, dict) else None
        if not isinstance(usage, dict):
            return {}
        out: dict[str, Any] = {}
        if isinstance(usage.get("prompt_tokens"), (int, float)):
            out["prompt_tokens"] = int(usage["prompt_tokens"])
        if isinstance(usage.get("completion_tokens"), (int, float)):
            out["completion_tokens"] = int(usage["completion_tokens"])
        if isinstance(usage.get("total_tokens"), (int, float)):
            out["total_tokens"] = int(usage["total_tokens"])
        # prompt_tokens_details.cached_tokens (OpenAI shape, emitted by
        # llama.cpp and SGLang) is the prompt-cache hit portion.
        details = (
            usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
        )
        if isinstance(details, dict) and isinstance(
            details.get("cached_tokens"), (int, float)
        ):
            out["cached_tokens"] = int(details["cached_tokens"])
        return out

    @staticmethod
    def _usage_need_host_fallback(
        usage_fields: dict[str, Any], instance: RegistryEntry
    ) -> bool:
        """Whether the host's last-generation metrics should fill usage gaps.

        Only cache-aware backends (llama.cpp, SGLang) can supply a cached
        split, so a HuggingFace instance never warrants the round-trip. The
        upstream ``usage`` block stays authoritative; the host only fills
        holes (``cached_tokens`` above all, but also a missing prompt or
        completion count).
        """
        if instance.backend_type not in {"llamacpp", "sglang"}:
            return False
        return (
            "prompt_tokens" not in usage_fields
            or "completion_tokens" not in usage_fields
            or "cached_tokens" not in usage_fields
        )

    @staticmethod
    def _clamp_cached_tokens(usage_fields: dict[str, Any]) -> dict[str, Any]:
        """Drop ``cached_tokens`` when it cannot be a real cache hit.

        A negative value or one exceeding ``prompt_tokens`` is a backend
        artifact; without a valid cached portion the row stays NULL rather
        than polluting the cache-hit rate with nonsense.
        """
        cached = usage_fields.get("cached_tokens")
        if cached is None:
            return usage_fields
        prompt = usage_fields.get("prompt_tokens")
        if (
            prompt is None
            or not isinstance(cached, int)
            or cached < 0
            or cached > prompt
        ):
            out = dict(usage_fields)
            out.pop("cached_tokens", None)
            return out
        return usage_fields

    async def _fill_usage_gaps(
        self,
        usage_fields: dict[str, Any],
        instance: RegistryEntry,
        *,
        within_s: int | None = None,
    ) -> dict[str, Any]:
        """Fill missing tokens from the host's last-generation metrics.

        The upstream usage block stays authoritative: the host only fills
        holes (``cached_tokens`` above all, but also a missing prompt or
        completion count). Only cache-aware backends (llama.cpp, SGLang) can
        supply a cached split, so HuggingFace never warrants the round-trip.

        When the only gap is the cached split and a recent host answer also
        lacked one, the instance is remembered as split-less and the
        round-trip is skipped until the negative cache expires -- old
        backends that never emit ``prompt_tokens_details`` would otherwise
        cost an HTTP call on every request. A failed fetch (``None``) never
        arms that cache: a transient blip or an out-of-window record is not
        proof that the backend lacks a split, so the next request gets a
        fresh chance to fill.
        """
        if not self._usage_need_host_fallback(usage_fields, instance):
            return usage_fields
        key = f"{instance.host_id}/{instance.instance_id}"
        only_cached_missing = (
            "prompt_tokens" in usage_fields
            and "completion_tokens" in usage_fields
            and "cached_tokens" not in usage_fields
        )
        if (
            only_cached_missing
            and self._no_cached_until.get(key, 0.0) > time.monotonic()
        ):
            return usage_fields
        if only_cached_missing and (
            within_s is None or within_s < _CACHED_FILL_WINDOW_S
        ):
            # Counts are already attributed upstream; only the split is
            # borrowed, so the strict concurrency window does not apply.
            within_s = _CACHED_FILL_WINDOW_S
        host_metrics = await self._fetch_last_generation_metrics(
            instance.host_id, instance.instance_id, within_s=within_s
        )
        if host_metrics is None:
            return usage_fields
        if "cached_tokens" not in host_metrics:
            self._no_cached_until[key] = time.monotonic() + _NEGATIVE_CACHE_TTL_S
        return {**host_metrics, **usage_fields}

    async def _fetch_last_generation_metrics(
        self, host_id: str, instance_id: str, within_s: int | None = None
    ) -> dict[str, Any] | None:
        """Fetch the host's last-generation metrics.

        ``None`` means the call failed (timeout, non-200, no host/session) and
        is distinct from a successful answer that simply lacks a cached split.
        Callers use the distinction: a transient failure must not be taken as
        proof that the backend cannot supply ``cached_tokens``.
        """
        try:
            await self._ensure_session()
            if not self.session:
                return None
            host = await host_db.get_host(host_id)
            if not host:
                return None
            url = f"{host.url}/instances/{instance_id}/last-generation"
            if within_s is not None and within_s >= 0:
                url += f"?within_s={int(within_s)}"
            headers = {"X-API-Key": host.api_key}
            timeout = aiohttp.ClientTimeout(total=3)
            async with self.session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                out: dict[str, Any] = {}
                if isinstance(data.get("prompt_tokens"), (int, float)):
                    out["prompt_tokens"] = int(data["prompt_tokens"])
                if isinstance(data.get("generated_tokens"), (int, float)):
                    out["completion_tokens"] = int(data["generated_tokens"])
                if isinstance(data.get("cached_tokens"), (int, float)):
                    out["cached_tokens"] = int(data["cached_tokens"])
                if isinstance(data.get("prompt_eval_tokens"), (int, float)):
                    out["prompt_eval_tokens"] = int(data["prompt_eval_tokens"])
                if isinstance(data.get("total_tokens"), (int, float)):
                    out["total_tokens"] = int(data["total_tokens"])
                elif "prompt_tokens" in out and "completion_tokens" in out:
                    out["total_tokens"] = (
                        out["prompt_tokens"] + out["completion_tokens"]
                    )
                if data.get("source"):
                    out["source"] = str(data["source"])
                if isinstance(data.get("decode_tps"), (int, float)):
                    out["decode_tps"] = float(data["decode_tps"])
                if isinstance(data.get("decode_ms_per_token"), (int, float)):
                    out["decode_ms_per_token"] = float(data["decode_ms_per_token"])
                return out
        except Exception:  # noqa: BLE001
            return None

    async def _fetch_instance_context_size(self, instance: RegistryEntry) -> int | None:
        """Fetch ctx_size from solar-host when the cached registry entry lacks it."""
        if instance.backend_type != "llamacpp":
            return None
        try:
            await self._ensure_session()
            if not self.session:
                return None
            host = await host_db.get_host(instance.host_id)
            if not host:
                return None
            url = f"{host.url}/instances/{instance.instance_id}"
            headers = {"X-API-Key": host.api_key}
            timeout = aiohttp.ClientTimeout(total=3)
            async with self.session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return RegistryEntry._extract_context_size(data)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _upstream_body(
        instance: RegistryEntry,
        data: dict[str, Any],
        *,
        stream: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Translate the request's ``model`` to the name the backend serves.

        Solar routes on the alias, but a backend cannot always be launched
        under it: SGLang reads ``a:b`` as base model ``a`` plus LoRA adapter
        ``b`` and answers "LoRA adapter b is not loaded", so the host serves a
        colon-free name and reports it. Forwarding the alias verbatim would
        turn every request to such an instance into a 400.

        Backends that do serve the alias (llama.cpp, HuggingFace) get the body
        untouched, including a partial name the client used — rewriting that
        would change long-standing behaviour for them.

        With *stream* the caller is the streaming relay, which can read the
        terminal ``usage`` block only when the upstream is asked to include
        it: ``stream_options.include_usage`` is merged into the body when the
        client did not already set it. The second return value reports
        whether this call injected it — the relay strips the usage chunk in
        that case, because the client never asked for it.

        Returns:
            Tuple of ``(body, injected_usage)``.
        """
        served = instance.served_model_name
        body = data
        if served and served != instance.model_alias and "model" in data:
            body = {**data, "model": served}

        injected = False
        if stream:
            existing = body.get("stream_options") if isinstance(body, dict) else None
            if not isinstance(existing, dict) or not existing.get("include_usage"):
                body = {
                    **body,
                    "stream_options": {
                        **(existing if isinstance(existing, dict) else {}),
                        "include_usage": True,
                    },
                }
                injected = True

        return body, injected

    def _sse_usage(self, payload: bytes) -> tuple[dict[str, Any], bool] | None:
        """Extract the OpenAI usage block from one SSE *data:* event.

        Returns None for events without a JSON payload carrying a ``usage``
        object (content chunks, ``[DONE]``, ping lines). The second tuple
        element reports whether the chunk is a *usage-only* terminal chunk
        (``choices`` is an empty list, per the OpenAI contract): a backend
        that attaches usage to a content-carrying or finish chunk is not
        usage-only, and its content must reach the client.
        """
        text = payload.decode("utf-8", "replace")
        data_value: str | None = None
        for raw in text.splitlines():
            if raw.startswith("data:"):
                data_value = raw[5:].lstrip()
                break
        if data_value is None or data_value == "[DONE]":
            return None
        try:
            parsed = json.loads(data_value)
        except (ValueError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        usage = parsed.get("usage")
        if not isinstance(usage, dict) or not usage:
            return None
        fields = self._extract_usage_from_result(parsed)
        if not fields:
            return None
        choices = parsed.get("choices")
        usage_only = isinstance(choices, list) and len(choices) == 0
        return fields, usage_only

    @staticmethod
    def _restore_alias_in_models(
        payload: dict[str, Any], instance: RegistryEntry
    ) -> dict[str, Any]:
        """Advertise the alias for an instance serving under a different name.

        The upstream reports what it was launched with, and that name does not
        route: :meth:`_resolve_model_name` only matches registry keys, which are
        aliases. Left alone, ``/v1/models`` would advertise a name whose next
        request 404s.
        """
        served = instance.served_model_name
        alias = instance.model_alias
        if not served or served == alias:
            return payload

        patched = dict(payload)
        # Ollama-style entries name the model twice; OpenAI-style entries once.
        for key, name_fields in (("models", ("name", "model")), ("data", ("id",))):
            entries = patched.get(key)
            if not isinstance(entries, list):
                continue
            patched[key] = [
                (
                    {**e, **{f: alias for f in name_fields if e.get(f) == served}}
                    if isinstance(e, dict)
                    else e
                )
                for e in entries
            ]
        return patched

    @staticmethod
    def _override_context_metadata(
        model: dict[str, Any], context_size: int | None
    ) -> dict[str, Any]:
        if not context_size:
            return model

        updated = dict(model)
        meta = dict(updated.get("meta") or {})
        if meta:
            meta["n_ctx_train"] = context_size
            meta["ctx_size"] = context_size
            updated["meta"] = meta

        details = updated.get("details")
        if isinstance(details, dict):
            updated["details"] = {
                **details,
                "context_length": context_size,
            }

        return updated

    async def get_available_models(
        self, model_patterns: list[str] | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """Aggregate /v1/models from all registered upstream instances.

        Returns a dict with both an Ollama-style ``models`` array and an
        OpenAI-style ``data`` array (matching llama.cpp's exact response shape).
        Both arrays carry a ``capabilities`` field on each entry, which is what
        downstream apps (Orchestrator, etc.) use to differentiate text-only
        from multimodal models. For llama.cpp upstreams the ``data[*]`` entry
        has no ``capabilities``, so we copy it over from the parallel
        ``models[*]`` entry (matched by ``name == id``).

        ``model_patterns`` (model scope) restricts which registry aliases are
        advertised; ``None`` keeps the response unrestricted.
        """
        await self._ensure_session()
        if not self.session:
            return {"models": [], "data": []}

        registry = await registry_store.get_registry()
        if model_patterns is not None:
            allowed = set(filter_aliases_for_patterns(model_patterns, registry))
            registry = {
                alias: insts for alias, insts in registry.items() if alias in allowed
            }
        data_dict: dict[str, dict[str, Any]] = {}
        ollama_dict: dict[str, dict[str, Any]] = {}

        for alias, instances in registry.items():
            if not instances:
                continue
            instance = instances[0]
            context_size = instance.context_size
            if context_size is None:
                context_size = await self._fetch_instance_context_size(instance)
            try:
                url = f"{instance.url}/v1/models"
                headers = {"Authorization": f"Bearer {instance.api_key}"}
                async with self.session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status == 200:
                        payload = self._restore_alias_in_models(
                            await response.json(), instance
                        )

                        ollama_models = payload.get("models") or []
                        caps_by_name: dict[str, list[str]] = {}
                        for m in ollama_models:
                            if not isinstance(m, dict):
                                continue
                            name = m.get("name") or m.get("model")
                            if name and name not in ollama_dict:
                                ollama_dict[name] = self._override_context_metadata(
                                    m, context_size
                                )
                            caps = m.get("capabilities")
                            if name and isinstance(caps, list):
                                caps_by_name[name] = caps

                        oai_data = payload.get("data") or []
                        for model in oai_data:
                            if not isinstance(model, dict):
                                continue
                            model_id = model.get("id", alias)
                            if "capabilities" not in model and model_id in caps_by_name:
                                model = {
                                    **model,
                                    "capabilities": caps_by_name[model_id],
                                }
                            model = self._override_context_metadata(model, context_size)
                            if model_id not in data_dict:
                                data_dict[model_id] = model
            except Exception:  # noqa: BLE001
                if alias not in data_dict:
                    fallback_caps = self._fallback_capabilities(instance.backend_type)
                    created = int(datetime.now(timezone.utc).timestamp())
                    data_dict[alias] = {
                        "id": alias,
                        "object": "model",
                        "created": created,
                        "owned_by": "solar",
                        "capabilities": fallback_caps,
                    }
                    data_dict[alias] = self._override_context_metadata(
                        data_dict[alias], context_size
                    )
                    if alias not in ollama_dict:
                        ollama_dict[alias] = {
                            "name": alias,
                            "model": alias,
                            "modified_at": "",
                            "size": "",
                            "digest": "",
                            "type": "model",
                            "description": "",
                            "tags": [""],
                            "capabilities": fallback_caps,
                            "parameters": "",
                            "details": {
                                "parent_model": "",
                                "format": "",
                                "family": "",
                                "families": [""],
                                "parameter_size": "",
                                "quantization_level": "",
                            },
                        }
                        ollama_dict[alias] = self._override_context_metadata(
                            ollama_dict[alias], context_size
                        )

        return {
            "models": list(ollama_dict.values()),
            "data": list(data_dict.values()),
        }

    @staticmethod
    def _fallback_capabilities(backend_type: str) -> list[str]:
        """Best-effort capabilities when upstream /v1/models is unreachable."""
        if backend_type == "huggingface_classification":
            return ["classification"]
        if backend_type == "huggingface_embedding":
            return ["embedding"]
        return ["completion"]

    async def _resolve_model_name(
        self, model: str, model_patterns: list[str] | None = None
    ) -> str | None:
        registry = await registry_store.get_registry()
        if model_patterns is not None:
            allowed = set(filter_aliases_for_patterns(model_patterns, registry))
            registry = {
                alias: insts for alias, insts in registry.items() if alias in allowed
            }
        if registry.get(model):
            return model
        matching = [m for m in registry if m.startswith(model) and registry[m]]
        if matching:
            return min(matching)
        return None

    def _parse_model_size(self, alias: str) -> float | None:
        try:
            size_token = alias.rsplit(":", 1)[-1] if ":" in alias else alias
            match = re.fullmatch(
                r"(?:(\d+)\s*x\s*)?(\d+(?:\.\d+)?)\s*([bBmM])", size_token
            )
            if not match:
                return None
            multiplier_str, value_str, unit = match.groups()
            multiplier = int(multiplier_str) if multiplier_str else 1
            value = float(value_str)
            if unit.lower() == "b":
                return multiplier * value
            if unit.lower() == "m":
                return multiplier * (value / 1000.0)
            return None
        except Exception:  # noqa: BLE001
            return None

    async def _get_next_instance(
        self,
        model: str,
        *,
        exclude_keys: set[str] | None = None,
        required_endpoint: str | None = None,
        model_patterns: list[str] | None = None,
    ) -> RegistryEntry | None:
        """Select the best instance for a model using host-aware load balancing."""
        resolved_model = await self._resolve_model_name(model, model_patterns)
        if not resolved_model:
            return None

        registry = await registry_store.get_registry()
        available = registry.get(resolved_model, [])
        if not available:
            return None

        if required_endpoint:
            available = [
                inst
                for inst in available
                if required_endpoint in inst.supported_endpoints
            ]
            if not available:
                return None

        healthy: list[RegistryEntry] = []
        fallback: list[RegistryEntry] = []
        for inst in available:
            ikey = f"{inst.host_id}-{inst.instance_id}"
            if exclude_keys and ikey in exclude_keys:
                continue
            is_h = await health_store.is_healthy(
                inst.host_id, inst.instance_id, health_ttl_s=settings.health_ttl_s
            )
            if is_h:
                healthy.append(inst)
            else:
                fallback.append(inst)

        candidates = healthy if healthy else fallback
        if not candidates:
            return None

        host_to_instances: dict[str, list[RegistryEntry]] = defaultdict(list)
        for inst in candidates:
            host_to_instances[inst.host_id].append(inst)

        candidate_host_ids = list(host_to_instances.keys())

        free_hosts: list[str] = []
        for hid in candidate_host_ids:
            count = await routing_store.get_host_active(hid)
            if count == 0:
                free_hosts.append(hid)

        if free_hosts:
            host_names = dict(
                zip(
                    free_hosts,
                    await asyncio.gather(
                        *[self._get_host_name(hid) for hid in free_hosts]
                    ),
                )
            )
            chosen_host = min(free_hosts, key=lambda h: host_names.get(h, h))
        else:
            host_weights: dict[str, float] = {}
            for hid in candidate_host_ids:
                host_weights[hid] = await routing_store.get_weight(hid)

            min_weight = min(host_weights.values()) if host_weights else 0.0
            min_hosts = [hid for hid, w in host_weights.items() if w == min_weight]

            host_names = dict(
                zip(
                    min_hosts,
                    await asyncio.gather(
                        *[self._get_host_name(hid) for hid in min_hosts]
                    ),
                )
            )
            chosen_host = min(min_hosts, key=lambda h: host_names.get(h, h))

        host_insts = host_to_instances[chosen_host]
        if len(host_insts) == 1:
            return host_insts[0]

        min_active = float("inf")
        best: list[RegistryEntry] = []
        for inst in host_insts:
            count = await routing_store.get_active(inst.host_id, inst.instance_id)
            if count < min_active:
                min_active = count
                best = [inst]
            elif count == min_active:
                best.append(inst)

        if len(best) == 1:
            return best[0]

        rr_idx = await routing_store.next_rr_index(resolved_model)
        return best[rr_idx % len(best)]

    async def _get_host_name(self, host_id: str) -> str:
        h = await host_db.get_host(host_id)
        return h.name if h and h.name else host_id

    # ── Routing infrastructure ────────────────────────────────

    async def _broadcast_routing_event(
        self, event_data: dict[str, Any], *, endpoint_id: str | None = None
    ) -> None:
        """Broadcast a routing event to WebUI via Socket.IO and log to database."""
        from dataclasses import asdict

        from app.database.logs import gateway_logger
        from app.socketio_app.webui_handlers import (
            broadcast_gateway_request,
            broadcast_to_webui,
        )

        try:
            summary = await gateway_logger.log_event(
                event_data, endpoint_id=endpoint_id
            )
            if summary:
                await broadcast_gateway_request(asdict(summary))
        except Exception as e:  # noqa: BLE001
            logger.error("Logging error: %s", e)

        try:
            event_type = event_data.get("type", "unknown")
            data = dict(event_data.get("data", {}))
            if endpoint_id is not None:
                data["endpoint_id"] = endpoint_id
            await broadcast_to_webui(event_type, data)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to broadcast routing event to WebUI: %s", e)

    def _ts(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    async def _emit_success(
        self,
        request_id: str,
        model: str,
        instance: RegistryEntry,
        duration: float,
        usage_fields: dict[str, Any],
        endpoint_id: str | None,
    ) -> None:
        base_data: dict[str, Any] = {
            "request_id": request_id,
            "model": model,
            "host_id": instance.host_id,
            "instance_id": instance.instance_id,
            "duration": duration,
            "timestamp": self._ts(),
        }
        base_data.update(self._clamp_cached_tokens(usage_fields))
        await self._broadcast_routing_event(
            {"type": "request_success", "data": base_data},
            endpoint_id=endpoint_id,
        )

    async def _emit_error(
        self,
        request_id: str,
        model: str,
        error_message: str,
        duration: float,
        endpoint_id: str | None,
        instance: RegistryEntry | None = None,
        client_ip: str | None = None,
    ) -> None:
        data: dict[str, Any] = {
            "request_id": request_id,
            "model": model,
            "error_message": error_message,
            "duration": duration,
            "timestamp": self._ts(),
        }
        if instance:
            data["host_id"] = instance.host_id
            data["instance_id"] = instance.instance_id
        if client_ip:
            data["client_ip"] = client_ip
        await self._broadcast_routing_event(
            {"type": "request_error", "data": data},
            endpoint_id=endpoint_id,
        )

    async def _emit_reroute(
        self,
        request_id: str,
        model: str,
        instance: RegistryEntry,
        attempt: int,
        endpoint_id: str | None,
    ) -> None:
        await self._broadcast_routing_event(
            {
                "type": "request_reroute",
                "data": {
                    "request_id": request_id,
                    "model": model,
                    "host_id": instance.host_id,
                    "instance_id": instance.instance_id,
                    "reason": "connect_error",
                    "attempt": attempt,
                    "timestamp": self._ts(),
                },
            },
            endpoint_id=endpoint_id,
        )

    @asynccontextmanager
    async def _routing_context(
        self, instance: RegistryEntry, weight: float | None
    ) -> AsyncIterator[None]:
        """Track active routing state in Redis, cleaning up on exit."""
        await routing_store.increment_active(instance.host_id, instance.instance_id)
        await routing_store.increment_host_active(instance.host_id)
        if weight is not None:
            await routing_store.add_weight(instance.host_id, weight)
        try:
            yield
        finally:
            try:
                await routing_store.decrement_active(
                    instance.host_id, instance.instance_id
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to decrement instance active for %s/%s",
                    instance.host_id,
                    instance.instance_id,
                )
            try:
                await routing_store.decrement_host_active(instance.host_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Failed to decrement host active for %s",
                    instance.host_id,
                )
            if weight is not None:
                try:
                    await routing_store.remove_weight(instance.host_id, weight)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Failed to remove weight for %s",
                        instance.host_id,
                    )

    async def _find_instance_or_retry(
        self,
        model: str,
        filter_endpoint: str,
        attempted: set[str],
        retried_once_flag: list[bool],
        model_patterns: list[str] | None = None,
    ) -> RegistryEntry | None:
        """Try to find an instance, with one registry-refresh retry."""
        instance = await self._get_next_instance(
            model,
            exclude_keys=attempted,
            required_endpoint=filter_endpoint,
            model_patterns=model_patterns,
        )
        if instance:
            return instance

        if not retried_once_flag[0]:
            retried_once_flag[0] = True
            attempted.clear()
            try:
                await self.refresh_model_registry()
            except Exception:  # noqa: BLE001, S110
                pass
            delay = max(0.0, float(settings.route_retry_delay_s))
            if delay > 0:
                await asyncio.sleep(delay)
            return await self._get_next_instance(
                model,
                exclude_keys=attempted,
                required_endpoint=filter_endpoint,
                model_patterns=model_patterns,
            )
        return None

    def _make_route_timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(
            total=settings.route_total_timeout_s,
            connect=settings.route_connect_timeout_s,
        )

    # ── Public routing API ────────────────────────────────────

    async def route_request(
        self,
        model: str,
        endpoint: str,
        data: dict[str, Any],
        client_ip: str = "unknown",
        required_endpoint: str | None = None,
        endpoint_id: str | None = None,
        model_patterns: list[str] | None = None,
    ) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        start_time = time.time()

        await self._broadcast_routing_event(
            {
                "type": "request_start",
                "data": {
                    "request_id": request_id,
                    "model": model,
                    "endpoint": endpoint,
                    "client_ip": client_ip,
                    "timestamp": self._ts(),
                },
            },
            endpoint_id=endpoint_id,
        )

        await self._ensure_session()
        if not self.session:
            raise RuntimeError("Failed to create aiohttp session")

        attempted: set[str] = set()
        last_error: Exception | None = None
        retried_once = [False]
        filter_endpoint = required_endpoint or endpoint

        for attempt in range(max(1, int(settings.route_max_attempts))):
            instance = await self._find_instance_or_retry(
                model,
                filter_endpoint,
                attempted,
                retried_once,
                model_patterns,
            )
            if not instance:
                break

            instance_key = f"{instance.host_id}-{instance.instance_id}"
            attempted.add(instance_key)
            weight = self._parse_model_size(instance.model_alias)

            host = await host_db.get_host(instance.host_id)
            host_name = host.name if host else "unknown"

            async with self._routing_context(instance, weight):
                try:
                    await self._broadcast_routing_event(
                        {
                            "type": "request_routed",
                            "data": {
                                "request_id": request_id,
                                "model": model,
                                "resolved_model": instance.model_alias,
                                "host_id": instance.host_id,
                                "host_name": host_name,
                                "instance_id": instance.instance_id,
                                "instance_url": instance.url,
                                "client_ip": client_ip,
                                "timestamp": self._ts(),
                                "attempt": attempt + 1,
                            },
                        },
                        endpoint_id=endpoint_id,
                    )

                    url = f"{instance.url}{endpoint}"
                    headers = {
                        "Authorization": f"Bearer {instance.api_key}",
                        "Content-Type": "application/json",
                    }
                    timeout = self._make_route_timeout()

                    async with self.session.post(
                        url,
                        json=self._upstream_body(instance, data)[0],
                        headers=headers,
                        timeout=timeout,
                    ) as response:
                        if response.status == 200:
                            await health_store.mark_healthy(
                                instance.host_id,
                                instance.instance_id,
                                ttl_s=settings.health_ttl_s + 2,
                            )
                            result = await response.json()
                            duration = time.time() - start_time

                            usage_fields = self._extract_usage_from_result(result)
                            usage_fields = await self._fill_usage_gaps(
                                usage_fields, instance
                            )

                            await self._emit_success(
                                request_id,
                                model,
                                instance,
                                duration,
                                usage_fields,
                                endpoint_id,
                            )
                            return result
                        elif response.status in _RETRYABLE_STATUSES:
                            error_text = await response.text()
                            logger.warning(
                                "Retryable %d from %s: %s",
                                response.status,
                                instance.url,
                                error_text[:200],
                            )
                            await health_store.mark_failed(
                                instance.host_id,
                                instance.instance_id,
                                cooldown_s=settings.health_cooldown_s,
                            )
                            last_error = Exception(f"Upstream {response.status}")
                            await self._emit_reroute(
                                request_id,
                                model,
                                instance,
                                attempt + 1,
                                endpoint_id,
                            )
                            continue
                        else:
                            error_text = await response.text()
                            duration = time.time() - start_time
                            msg = f"Request failed: {response.status} - {error_text}"
                            await self._emit_error(
                                request_id,
                                model,
                                msg,
                                duration,
                                endpoint_id,
                                instance=instance,
                            )
                            raise ValueError(msg)

                except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
                    await health_store.mark_failed(
                        instance.host_id,
                        instance.instance_id,
                        cooldown_s=settings.health_cooldown_s,
                    )
                    last_error = e
                    await self._emit_reroute(
                        request_id, model, instance, attempt + 1, endpoint_id
                    )
                except Exception as e:
                    duration = time.time() - start_time
                    await self._emit_error(
                        request_id,
                        model,
                        str(e),
                        duration,
                        endpoint_id,
                        instance=instance,
                    )
                    raise

        error_msg = (
            f"Model '{model}' not found or no instances available"
            if not attempted
            else f"Failed to connect to model '{model}' after {len(attempted)} attempts: {last_error}"
        )
        await self._emit_error(
            request_id,
            model,
            error_msg,
            time.time() - start_time,
            endpoint_id,
            client_ip=client_ip,
        )
        if attempted:
            raise ValueError(error_msg)
        raise ValueError(error_msg)

    async def stream_request(
        self,
        model: str,
        endpoint: str,
        data: dict[str, Any],
        client_ip: str = "unknown",
        required_endpoint: str | None = None,
        endpoint_id: str | None = None,
        model_patterns: list[str] | None = None,
    ):
        request_id = str(uuid.uuid4())
        start_time = time.time()
        completed = False

        await self._broadcast_routing_event(
            {
                "type": "request_start",
                "data": {
                    "request_id": request_id,
                    "model": model,
                    "endpoint": endpoint,
                    "stream": True,
                    "client_ip": client_ip,
                    "timestamp": self._ts(),
                },
            },
            endpoint_id=endpoint_id,
        )

        await self._ensure_session()
        if not self.session:
            raise RuntimeError("Failed to create aiohttp session")

        filter_endpoint = required_endpoint or endpoint
        attempted: set[str] = set()
        last_error: Exception | None = None
        retried_once = [False]

        for attempt in range(max(1, int(settings.route_max_attempts))):
            instance = await self._find_instance_or_retry(
                model,
                filter_endpoint,
                attempted,
                retried_once,
                model_patterns,
            )
            if not instance:
                break

            instance_key = f"{instance.host_id}-{instance.instance_id}"
            attempted.add(instance_key)
            weight = self._parse_model_size(instance.model_alias)

            host = await host_db.get_host(instance.host_id)
            host_name = host.name if host else "unknown"

            async with self._routing_context(instance, weight):
                try:
                    await self._broadcast_routing_event(
                        {
                            "type": "request_routed",
                            "data": {
                                "request_id": request_id,
                                "model": model,
                                "resolved_model": instance.model_alias,
                                "host_id": instance.host_id,
                                "host_name": host_name,
                                "instance_id": instance.instance_id,
                                "instance_url": instance.url,
                                "client_ip": client_ip,
                                "timestamp": self._ts(),
                                "attempt": attempt + 1,
                            },
                        },
                        endpoint_id=endpoint_id,
                    )

                    url = f"{instance.url}{endpoint}"
                    headers = {
                        "Authorization": f"Bearer {instance.api_key}",
                        "Content-Type": "application/json",
                    }
                    timeout = self._make_route_timeout()

                    # Streaming usage is exact only when the upstream is asked
                    # to include it; the relay below captures the terminal
                    # chunk's usage and strips it when we injected the option
                    # ourselves (the client never asked for it).
                    body, injected_usage = self._upstream_body(
                        instance,
                        data,
                        stream=(endpoint in _STREAM_USAGE_ENDPOINTS),
                    )

                    async with self.session.post(
                        url,
                        json=body,
                        headers=headers,
                        timeout=timeout,
                    ) as response:
                        if response.status == 200:
                            await health_store.mark_healthy(
                                instance.host_id,
                                instance.instance_id,
                                ttl_s=settings.health_ttl_s + 2,
                            )
                            captured_usage: dict[str, Any] = {}
                            event_lines: list[bytes] = []
                            async for raw in response.content:
                                event_lines.append(raw)
                                if raw.strip() == b"":
                                    payload = b"".join(event_lines)
                                    usage_info = self._sse_usage(payload)
                                    if usage_info:
                                        usage, usage_only = usage_info
                                        captured_usage = usage
                                        if injected_usage and usage_only:
                                            # The client did not ask for
                                            # usage; drop the injected
                                            # usage-only terminal chunk.
                                            event_lines = []
                                            continue
                                    yield payload
                                    event_lines = []
                            if event_lines:
                                # Upstream ended without the trailing blank
                                # line; flush what was buffered.
                                yield b"".join(event_lines)

                            completed = True
                            duration = time.time() - start_time
                            if captured_usage:
                                # The terminal chunk can carry the counts but
                                # omit the cached split (older backends); ask
                                # the host for it, bounded to this window.
                                usage_fields = await self._fill_usage_gaps(
                                    captured_usage, instance, within_s=5
                                )
                            else:
                                # Fallback for backends that ignore
                                # stream_options: bound the attribution window
                                # so a stale generation from another request
                                # cannot be billed to this one.
                                usage_fields = (
                                    await self._fetch_last_generation_metrics(
                                        instance.host_id,
                                        instance.instance_id,
                                        within_s=5,
                                    )
                                ) or {}
                            await self._emit_success(
                                request_id,
                                model,
                                instance,
                                duration,
                                usage_fields,
                                endpoint_id,
                            )
                            return
                        elif response.status in _RETRYABLE_STATUSES:
                            error_text = await response.text()
                            logger.warning(
                                "Retryable %d from %s: %s",
                                response.status,
                                instance.url,
                                error_text[:200],
                            )
                            await health_store.mark_failed(
                                instance.host_id,
                                instance.instance_id,
                                cooldown_s=settings.health_cooldown_s,
                            )
                            last_error = Exception(f"Upstream {response.status}")
                            await self._emit_reroute(
                                request_id,
                                model,
                                instance,
                                attempt + 1,
                                endpoint_id,
                            )
                            continue
                        else:
                            error_text = await response.text()
                            duration = time.time() - start_time
                            msg = f"Request failed: {response.status} - {error_text}"
                            await self._emit_error(
                                request_id,
                                model,
                                msg,
                                duration,
                                endpoint_id,
                                instance=instance,
                            )
                            raise ValueError(msg)

                except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
                    await health_store.mark_failed(
                        instance.host_id,
                        instance.instance_id,
                        cooldown_s=settings.health_cooldown_s,
                    )
                    last_error = e
                    await self._emit_reroute(
                        request_id, model, instance, attempt + 1, endpoint_id
                    )
                except GeneratorExit:
                    if not completed:
                        try:
                            await self._emit_error(
                                request_id,
                                model,
                                "Client disconnected",
                                time.time() - start_time,
                                endpoint_id,
                                instance=instance,
                                client_ip=client_ip,
                            )
                        except Exception:  # noqa: BLE001, S110
                            pass
                    return
                except Exception as e:
                    duration = time.time() - start_time
                    await self._emit_error(
                        request_id,
                        model,
                        str(e),
                        duration,
                        endpoint_id,
                        instance=instance,
                    )
                    raise

        error_msg = (
            f"Model '{model}' not found or no instances available"
            if not attempted
            else f"Failed to connect to model '{model}' after {len(attempted)} attempts: {last_error}"
        )
        await self._emit_error(
            request_id,
            model,
            error_msg,
            time.time() - start_time,
            endpoint_id,
            client_ip=client_ip,
        )
        if attempted:
            raise ValueError(error_msg)
        raise ValueError(error_msg)


gateway = OpenAIGateway()
