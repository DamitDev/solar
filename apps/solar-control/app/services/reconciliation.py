"""Intent reconciliation engine (S-041).

Level-triggered reconciliation loop that compares desired state
(intents) with observed state (instances + gateway registry) and
converges the cluster by creating, stopping, and migrating instances.

Design:
- Periodic tick (configurable interval) + event-driven wake-ups.
- Per-intent Redis lock prevents concurrent reconciliation from
  multiple Solar Control replicas.
- Reuses shared placement policy (app.services.placement) and
  migration orchestrator (app.services.migration).
- Stateless/restart-safe: recomputes managed(I) from observed state
  on every pass; never trusts memory alone.
"""

import asyncio
import fnmatch
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.config import settings
from app.redis_state.connection import redis_client
from app.redis_state.hosts import PULLS_MAP

logger = logging.getLogger(__name__)

# Per-intent lock key prefix and TTL.
# _LOCK_TTL auto-expires so a crashed reconciler doesn't block forever.
_LOCK_PREFIX = "solar:reconcile:lock:"
_LOCK_TTL = 30


# ── Action model ───────────────────────────────────────────────


class ActionType:
    CREATE = "create"
    STOP = "stop"
    REPLACE = "replace"
    RECREATE = "recreate"
    MIGRATE = "migrate"
    EVACUATE = "evacuate"
    DISOWN = "disown"
    NOOP = "noop"


@dataclass
class Action:
    """A single reconciliation action to execute on a host."""

    type: str
    intent_id: str
    alias: str
    host_id: str | None = None
    host_name: str | None = None
    instance_id: str | None = None  # for stop / replace / recreate / migrate / disown
    target_host_id: str | None = None  # for migrate/evacuate: where to move it
    target_host_name: str | None = None  # for migrate/evacuate
    reason: str = ""
    priority: int = 0  # lower executes first (stops before creates)


# Exponential backoff bounds (seconds)
_BACKOFF_MIN_S = 10
_BACKOFF_MAX_S = 300


class StartOutcomeUnknown(HTTPException):
    """A start call the host never answered, so the outcome is unknown.

    Not the same as a host that answered with an error. The host's start
    blocks while it launches the server and retries, so a client timeout or a
    dropped connection says nothing about whether the instance is coming up —
    and under load it usually is. Nothing may be deleted on this failure: the
    next observation reports what actually happened, whereas deleting would
    destroy a replica mid-start and pay for the cold start again.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=504, detail=detail)


class InstanceStartFailed(HTTPException):
    """A start call the host answered with a structured failure body (C2).

    Carries the instance id, the child exit code and the log tail the host
    attached to its error response, so the recorded ``last_error`` can link
    the failure to its process logs. Keeps the human-readable 502 detail for
    backwards compatibility.
    """

    def __init__(
        self,
        *,
        detail: str,
        instance_id: str | None = None,
        exit_code: int | None = None,
        log_tail: list[str] | None = None,
    ) -> None:
        super().__init__(status_code=502, detail=detail)
        self.instance_id = instance_id
        self.exit_code = exit_code
        self.log_tail = log_tail


def _parse_start_failure_body(
    text: str,
) -> tuple[str, str | None, int | None, list[str] | None]:
    """Parse a host's start-failure body into (message, instance_id, exit, tail).

    Hosts are deployed separately from control, so three shapes must all be
    understood (C2):

    * flat — ``{"detail": "...", "instance_id": ..., "exit_code": N,
      "log_tail": [...]}``, what a current host returns;
    * nested — ``{"detail": {"detail": "...", "instance_id": ...}}``, which is
      what FastAPI produces when the payload is raised as an
      ``HTTPException`` detail rather than returned as a ``JSONResponse``;
    * legacy — ``{"detail": "some string"}`` or a non-JSON body, from hosts
      predating the structured payload.

    The returned message is always a ``str``: interpolating a parsed dict into
    the human-readable 502 would leak a Python ``repr`` to the user.
    """
    try:
        body: Any = json.loads(text)
    except (ValueError, TypeError):
        return text, None, None, None

    if not isinstance(body, dict):
        return (body if isinstance(body, str) else text), None, None, None

    # Unwrap one level of FastAPI's HTTPException nesting.
    inner = body.get("detail")
    if isinstance(inner, dict):
        body = inner

    detail = body.get("detail")
    message = detail if isinstance(detail, str) else text

    instance_id = body.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        instance_id = None

    exit_code = body.get("exit_code")
    # bool is an int subclass; an exit code is never a boolean.
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        exit_code = None

    log_tail = body.get("log_tail")
    if isinstance(log_tail, list):
        log_tail = [line if isinstance(line, str) else str(line) for line in log_tail]
    else:
        log_tail = None

    return message, instance_id, exit_code, log_tail


# Hard bound on a single reconciliation action. Host calls are individually
# time-bounded, but a MIGRATE can chain several (pull with ORAS retries,
# create, start, delete) and stall the whole loop for minutes; the bound
# turns such stalls into a recorded failure + paced backoff instead (§8.3).
_ACTION_TIMEOUT_S = 60

# Cold-start-capable actions (CREATE / EVACUATE / MIGRATE) may pull a
# multi-GB model and block on the host's start until the backend reports
# readiness — minutes, legitimately. Their bound covers the whole window
# (pull + start + margin) so a cold start completes in one attempt instead
# of recording a premature TimeoutError. Quick ops keep the short bound so
# a stalled host never freezes the loop.
_COLD_START_ACTIONS = frozenset(
    {ActionType.CREATE, ActionType.EVACUATE, ActionType.MIGRATE}
)


def _action_timeout_s(action: Action) -> float:
    """Return the timeout bound for *action* (see _ACTION_TIMEOUT_S)."""
    if action.type in _COLD_START_ACTIONS:
        return (
            settings.model_pull_timeout_s
            + settings.host_start_timeout_s
            + _ACTION_TIMEOUT_S
        )
    return _ACTION_TIMEOUT_S


# C4: latest pull progress per (host, model source), pushed by the host
# over the WS channel and rebroadcast by control (see host_handlers). The
# key constant lives in app.redis_state.hosts (PULLS_MAP).


def _entry_age_s(at: Any) -> float | None:
    """Age in seconds of an ISO-8601 ``at`` stamp, or None if unusable.

    ``at`` is written by control (see ``host_pull_progress`` and
    ``set_host_resource_snapshot``), but an entry persisted by an older build
    may be naive. Subtracting a naive from an aware datetime raises
    ``TypeError``, so the naive case is assumed UTC rather than allowed to
    propagate out of a freshness check.
    """
    if not isinstance(at, str):
        return None
    try:
        at_dt = datetime.fromisoformat(at)
        if at_dt.tzinfo is None:
            at_dt = at_dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - at_dt).total_seconds()
    except (ValueError, TypeError):
        return None


async def _pull_progress_fresh(host_id: str | None, source_uri: str) -> bool:
    """True when the host recently reported pull progress for the URI (C4).

    Fresh means the entry's ``at`` timestamp — control's receive time — is
    younger than ``settings.pull_progress_stale_after_s``. Any Redis/parse
    failure reads as stale, which degrades the progress-aware wait to the
    plain bound.

    A terminal entry (``completed``/``failed``) is never fresh: the pull is
    over, so there is no progress left to wait for.
    """
    if not host_id or not source_uri:
        return False
    try:
        r = redis_client()
        raw = await r.hget(PULLS_MAP, f"{host_id}|{source_uri}")
    except Exception:  # noqa: BLE001
        return False
    if not raw:
        return False
    try:
        entry = json.loads(raw)
    except (ValueError, TypeError):
        return False
    if not isinstance(entry, dict):
        return False
    data = entry.get("data")
    if isinstance(data, dict) and data.get("phase") in ("completed", "failed"):
        return False
    age = _entry_age_s(entry.get("at"))
    if age is None:
        return False
    return age <= settings.pull_progress_stale_after_s


async def _await_action_with_progress(coro: Any, action: Action, intent: Any) -> Any:
    """Await *coro* with a progress-aware bound (C4, fixes the 60 s trap).

    Cold-start actions (CREATE/EVACUATE/MIGRATE) are bounded by
    ``_action_timeout_s(action)`` — but that bound can still fire
    mid-download on a slow link. Instead of one big ``wait_for``, wait in
    slices of ``settings.action_progress_slice_s``; at each slice boundary
    the host's pull progress for ``(action.host_id, intent.model_source)``
    is checked, and the wait continues while that progress is newer than
    ``settings.pull_progress_stale_after_s``. The hard ceiling remains
    ``_action_timeout_s(action)``.

    When the ceiling is hit while the host is *still* downloading, the
    raised ``TimeoutError`` carries ``recoverable=True`` so the recorded
    ``last_error`` tells the webui the deployment is still working rather
    than dead.

    The slice boundary is decided on *task state*, never on exception type:
    ``asyncio.TimeoutError`` is the builtin ``TimeoutError`` on 3.11+ and
    aiohttp's timeouts subclass it, so treating a raised timeout as a slice
    expiry would re-await an already-finished task in a tight loop and
    swallow the real error. ``asyncio.wait`` leaves the task running on
    timeout, so a slice boundary never cancels a host call mid-flight.
    """
    if action.type not in _COLD_START_ACTIONS:
        return await asyncio.wait_for(coro, timeout=_ACTION_TIMEOUT_S)

    deadline = time.monotonic() + _action_timeout_s(action)
    task = asyncio.ensure_future(coro)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            slice_s = min(settings.action_progress_slice_s, remaining)
            done, _pending = await asyncio.wait({task}, timeout=slice_s)
            if done:
                # Returns the action's result, or re-raises its exception
                # unchanged — including a genuine TimeoutError from a host
                # call, which must not be mistaken for a slice expiry.
                return task.result()
            if not await _pull_progress_fresh(action.host_id, intent.model_source):
                break

        still_working = await _pull_progress_fresh(action.host_id, intent.model_source)
        exc = asyncio.TimeoutError(
            f"Action {action.type} for intent {intent.alias} exceeded its bound "
            f"({_action_timeout_s(action):.0f}s)"
            + (
                f" while the host is still downloading {intent.model_source}"
                if still_working
                else ""
            )
        )
        exc.recoverable = still_working  # type: ignore[attr-defined]
        raise exc
    finally:
        # Covers both giving up above and an outer cancellation (reconciler
        # shutdown): without this the task would keep running detached.
        if not task.done():
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 — CancelledError and task errors alike
                logger.debug(
                    "Cancelled in-flight %s action task for intent %s",
                    action.type,
                    getattr(intent, "alias", "?"),
                )


# How long the *displaced* intent is left alone after a displacement
# MIGRATE/stop so it does not race-recreate the instance the migration is
# moving (§8.5: coordinated displacement, not a fight).
_SETTLE_S = float(os.getenv("RECONCILE_SETTLE_S", "3.0"))
_MIGRATE_SETTLE_S = float(os.getenv("RECONCILE_MIGRATE_SETTLE_S", "10.0"))
_DISPLACE_COOLDOWN_S = float(os.getenv("RECONCILE_DISPLACE_COOLDOWN_S", "60.0"))

# Redis set of instance ids whose ownership markers were cleared while the
# instance kept running (orphan delete, §12.4). The host config retains the
# markers (no host-side PATCH for running instances), so without this set a
# cache re-seed after a control restart would make the reconciler treat the
# orphan as managed again.
_DISOWNED_SET = "solar:disowned"


# ── Reconciler ─────────────────────────────────────────────────


class Reconciler:
    """Periodic + event-driven intent reconciliation engine."""

    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._wake_event = asyncio.Event()
        self._running = False
        # Per-intent exponential backoff: {intent_id: {"failures": N, "next_retry_at": iso}}
        self._backoff: dict[str, dict[str, Any]] = {}
        # Per-intent settle window (monotonic deadline) after CREATE/MIGRATE
        # so the host's WS instances_update lands before the next diff —
        # otherwise the stale observed state diffs a duplicate CREATE whose
        # start is then SIGTERM'd by the surplus cleanup (-15/404 races).
        self._settle_until: dict[str, float] = {}
        # Per-instance displacement cooldown (monotonic deadline): a MIGRATE
        # that found no target (staging left in place, or ephemeral already
        # stopped) must not be re-attempted every tick — it starves the
        # intent's CREATE actions and cannot free capacity it already failed
        # to free (§8.5 partial fulfillment instead of thrash).
        self._displace_cooldown: dict[str, float] = {}
        # C5/5.7: full instance configs captured during a pending-edit
        # deep-compare window, keyed (intent_id, instance_id, spec_version).
        # The real config does not change inside the window, so repeated
        # ticks must not re-fetch it; a new spec edit changes the key and
        # drops the intent's stale entries.
        self._config_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._config_cache_spec: dict[str, str] = {}
        # C3: spec version whose fleet violations have already been logged, so
        # a legacy spec is reported once rather than on every tick forever.
        self._fleet_violations_logged: dict[str, str | None] = {}

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background reconciliation loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Reconciler started (interval=%.1fs)", settings.reconcile_interval_s
        )

    async def stop(self) -> None:
        """Stop the background reconciliation loop."""
        self._running = False
        self._wake_event.set()  # unblock any sleep
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Reconciler stopped")

    def wake(self) -> None:
        """Trigger an immediate reconciliation pass (event-driven)."""
        self._wake_event.set()

    def settle_intent(self, intent_id: str, seconds: float) -> None:
        """Skip diffing *intent_id* for *seconds*.

        Used by flows that mutate an intent's instances outside the
        reconciler (e.g. the API migration path) so a tick cannot race
        a duplicate CREATE against a freshly placed instance whose WS
        push has not landed in the instance cache yet — the duplicate
        then surplus-stops (and deletes) the placed instance.
        """
        self._settle_until[intent_id] = time.monotonic() + seconds

    # ── Backoff ────────────────────────────────────────────────

    def _backoff_clear(self, intent_id: str) -> None:
        """Clear backoff state for *intent_id* after a successful tick."""
        self._backoff.pop(intent_id, None)

    def _backoff_record_failure(
        self, intent_id: str, *, spec_version: str | None = None
    ) -> None:
        """Record a failure and set the next retry time with exponential backoff.

        *spec_version* is the spec the failure belongs to, so a later edit
        can void the backoff instead of inheriting it (S-044).
        """
        now = datetime.now(timezone.utc)
        entry = self._backoff.get(intent_id, {"failures": 0})
        entry["failures"] = entry.get("failures", 0) + 1
        delay = min(_BACKOFF_MIN_S * (2 ** (entry["failures"] - 1)), _BACKOFF_MAX_S)
        entry["next_retry_at"] = (
            datetime.fromtimestamp(now.timestamp() + delay, tz=timezone.utc)
        ).isoformat()
        entry["spec_version"] = spec_version
        self._backoff[intent_id] = entry
        logger.debug(
            "Backoff for intent %s: failures=%d delay=%.0fs",
            intent_id,
            entry["failures"],
            delay,
        )

    def _backoff_void_on_spec_change(self, intent: Any) -> None:
        """Drop the backoff if the intent's spec changed since the failure.

        The delay was earned by a spec that no longer exists, and an edit is
        often the fix for whatever was failing, so it must be retried at
        once rather than inheriting the old spec's penalty (S-044 §12.5).
        """
        entry = self._backoff.get(intent.id)
        if entry and entry.get("spec_version") != _spec_version(intent):
            logger.debug(
                "Intent %s spec changed since its last failure, clearing backoff",
                intent.id,
            )
            self._backoff.pop(intent.id, None)

    def _log_fleet_violations(self, intent: Any, hosts: list[Any]) -> None:
        """Report hard fleet violations in an already-stored spec, once (C3).

        A spec written before these rules existed cannot be fixed from here —
        only the owner can edit it — so this is pure diagnostics. It runs off
        the roster ``_observe`` already fetched and logs once per spec version:
        an unfixed legacy spec must not fill the log at one line per tick.
        """
        # _spec_version is None for a settled intent — the common case — so
        # "never checked" has to be distinguished from "checked at version
        # None" rather than leaning on dict.get's default.
        version = _spec_version(intent)
        if (
            intent.id in self._fleet_violations_logged
            and self._fleet_violations_logged[intent.id] == version
        ):
            return
        try:
            from app.services.intent_validation import validate_intent_fleet_hard

            hard = validate_intent_fleet_hard(intent.model_dump(), hosts)
        except Exception:
            logger.warning(
                "Fleet validation skipped for intent %s", intent.id, exc_info=True
            )
            return
        self._fleet_violations_logged[intent.id] = version
        if hard:
            logger.error(
                "Intent %s (%s) violates fleet validation: %s",
                intent.id,
                intent.alias,
                hard,
            )

    def _backoff_active(self, intent_id: str) -> bool:
        """Return True if backoff is active and retry should be skipped."""
        entry = self._backoff.get(intent_id)
        if not entry:
            return False
        next_retry = entry.get("next_retry_at")
        if not next_retry:
            return False
        try:
            retry_at = datetime.fromisoformat(next_retry)
            return datetime.now(timezone.utc) < retry_at
        except (ValueError, TypeError):
            return False

    # ── Main loop ──────────────────────────────────────────────

    async def _loop(self) -> None:
        """Main loop: sleep → reconcile → repeat."""
        while self._running:
            try:
                await self._reconcile_all()
            except Exception:
                logger.exception("Reconciliation pass failed")

            # Wait for the next interval or an event-driven wake-up
            try:
                self._wake_event.clear()
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=settings.reconcile_interval_s,
                )
            except asyncio.TimeoutError:
                pass  # interval elapsed — normal

    async def _reconcile_all(self) -> None:
        """Fetch all active intents and reconcile each one."""
        from app.database.intents import intent_db
        from app.services.drain import sweep_drained_hosts

        intents = await intent_db.list_active_for_reconciliation()

        logger.debug("Reconciling %d active intent(s)", len(intents))
        for intent in intents:
            if not self._running:
                break

            # Acquire per-intent lock to avoid concurrent reconciliation
            lock_key = f"{_LOCK_PREFIX}{intent.id}"
            r = redis_client()
            acquired = await r.set(lock_key, "1", nx=True, ex=_LOCK_TTL)
            if not acquired:
                logger.debug("Intent %s locked by another replica, skipping", intent.id)
                continue
            try:
                # Reload under the lock: the list above was read before the
                # lock, so the spec may have been edited (S-044) or the intent
                # deleted since. Acting on the copy we listed would converge
                # toward a spec that no longer exists.
                fresh = await intent_db.get_intent(intent.id)
                if fresh is None:
                    continue
                await self._reconcile_one(fresh)
            except Exception:
                logger.exception("Reconciliation failed for intent %s", intent.id)
            finally:
                await r.delete(lock_key)

        # A drain finishes once its host is empty. Checked after the intents
        # so this tick's evacuations are already reflected (S-043 §4.4), and
        # unconditionally so a host with no intents at all can still drain.
        try:
            await sweep_drained_hosts()
        except Exception:
            logger.exception("Drain sweep failed")

    # ── Per-intent reconciliation ──────────────────────────────

    async def _reconcile_one(self, intent: Any) -> None:
        """Reconcile a single intent: observe → diff → act → update status.

        Implements deployment-intent.md §8.1 reconciliation loop.

        When a deployment strategy is in-flight (strategy_progress is set),
        delegates to the strategy state machine instead of the normal
        diff/act path (S-042 §11).
        """
        # An edit voids the failure backoff earned by the previous spec.
        # The reservation sweep runs first so releases happen even while
        # the intent is in backoff or settle.
        await self._release_finished_reservations(intent)
        self._backoff_void_on_spec_change(intent)
        if self._backoff_active(intent.id):
            logger.debug("Intent %s in backoff, skipping", intent.id)
            return

        # Settle window: skip diffing right after a CREATE/MIGRATE so the
        # host's WS push lands before we re-observe (closes the
        # duplicate-create window; still refresh status).
        if time.monotonic() < self._settle_until.get(intent.id, 0.0):
            logger.debug("Intent %s in settle window, skipping diff", intent.id)
            # A delete that raced the settle window falls through to the
            # normal (delete) flow; anything else only refreshes status. The
            # phase is trustworthy here because the intent was just reloaded.
            if _intent_phase(intent) != "deleting":
                await self._update_status(intent, await self._observe(intent))
                return

        # 1. Observe
        observed = await self._observe(intent)

        # C3 defensive fleet validation: a spec written before these rules
        # existed shows up in the logs instead of silently churning. Only the
        # hard half runs, off the roster _observe already fetched, and only
        # once per spec version — the advisory half reads a snapshot per host
        # and belongs on the write path, where the user can act on it.
        self._log_fleet_violations(intent, observed.get("hosts") or [])

        # ── S-042: Check if a strategy is already in-flight ──────
        strategy_progress = self._get_strategy_progress(intent)
        if strategy_progress is not None:
            await self._continue_strategy(intent, observed, strategy_progress)
            return

        # ── Delete / scale-to-zero takes priority over strategies ─
        current_phase = _intent_phase(intent)
        if current_phase == "deleting" or (
            current_phase not in ("deleting", "deleted") and intent.replicas == 0
        ):
            # Normal delete/scale-to-zero flow via _diff
            actions = self._diff(intent, observed)
            if not actions:
                await self._update_status(intent, observed)
                return
            actions.sort(key=lambda a: a.priority)
            action = actions[0]
            drift_replace = _is_drift_replace(action)
            last_error = None
            action_succeeded = False
            try:
                t_act = time.monotonic()
                result = await asyncio.wait_for(
                    self._act(intent, action), timeout=_action_timeout_s(action)
                )
                logger.debug(
                    "act %s for %s took %.1fs",
                    action.type,
                    intent.id[:8],
                    time.monotonic() - t_act,
                )
                action_succeeded = True
                if action.type in (ActionType.CREATE, ActionType.MIGRATE) and result:
                    await asyncio.sleep(0.5)
                    observed = await self._observe(intent)
                    self._settle_until[intent.id] = time.monotonic() + _SETTLE_S
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "Action %s failed for intent %s: %s",
                    action.type,
                    intent.id,
                    e,
                )
                last_error = {
                    "code": type(e).__name__,
                    "message": str(e),
                    "host_id": action.host_id,
                    "source_uri": intent.model_source,
                    "at": datetime.now(timezone.utc).isoformat(),
                    # C2: link the failure to its process logs when the host
                    # answered with a structured body.
                    "instance_id": getattr(e, "instance_id", None)
                    or action.instance_id,
                    "log_tail": getattr(e, "log_tail", None),
                    # C4: set when the action gave up while the host was
                    # still making progress (e.g. a cold-start pull).
                    "recoverable": bool(getattr(e, "recoverable", False)),
                }
            if action_succeeded and last_error is None:
                self._backoff_clear(intent.id)
            elif last_error is not None:
                self._backoff_record_failure(
                    intent.id, spec_version=_spec_version(intent)
                )
            t_upd = time.monotonic()
            await self._update_status(
                intent,
                observed,
                last_error=last_error,
                drift_replace=drift_replace,
            )
            logger.debug(
                "update_status for %s took %.1fs",
                intent.id[:8],
                time.monotonic() - t_upd,
            )
            return

        # 2. Diff
        actions = self._diff(intent, observed)

        # ── S-042: Check if a strategy should be initiated ────────
        strategy_progress_data = self._maybe_initiate_strategy(
            intent, observed, actions
        )
        if strategy_progress_data is not None:
            # Persist strategy_progress; next tick will execute it. No action
            # ran, so the drift counter does not move.
            await self._update_status(
                intent,
                observed,
                strategy_progress=strategy_progress_data,
            )
            return

        if not actions:
            # No actions needed — still update status to reflect current state.
            # Nothing to converge means every replica matches the spec, so a
            # pending spec change is done rolling out (S-044) — unless a
            # replica could not be compared against it at all.
            await self._update_status(
                intent,
                observed,
                spec_settled=_spec_settled(observed, actions),
            )
            return

        # 3. Act — execute at most one action per tick for gradual convergence
        # Actions are sorted by priority (stops first, creates last)
        actions.sort(key=lambda a: a.priority)
        action = actions[0]
        # Counted off the action actually executed, not every planned one: a
        # drift REPLACE that loses the priority sort is not an attempt, and
        # counting it would burn the breaker's budget without ever having
        # tried the replacement.
        drift_replace = _is_drift_replace(action)
        logger.info(
            "Reconciling intent %s (%s): action=%s reason=%s",
            intent.id,
            intent.alias,
            action.type,
            action.reason,
        )

        last_error = None
        action_succeeded = False
        try:
            # C4: cold-start actions use the progress-aware bound instead of
            # the raw 60 s — a shortfall CREATE pulls the model and blocks on
            # the host's start, which legitimately takes minutes.
            result = await _await_action_with_progress(
                self._act(intent, action), action, intent
            )
            action_succeeded = True

            # If we created/migrated, re-observe for fresh state
            if action.type in (ActionType.CREATE, ActionType.MIGRATE) and result:
                await asyncio.sleep(0.5)
                observed = await self._observe(intent)
                # Let the host's WS instances_update land before the next
                # diff (see _settle_until above).
                self._settle_until[intent.id] = time.monotonic() + _SETTLE_S
        except Exception as e:  # noqa: BLE001
            logger.error(
                "Action %s failed for intent %s: %s",
                action.type,
                intent.id,
                e,
            )
            last_error = {
                "code": type(e).__name__,
                "message": str(e),
                "host_id": action.host_id,
                "source_uri": intent.model_source,
                "at": datetime.now(timezone.utc).isoformat(),
                # C2: link the failure to its process logs when the host
                # answered with a structured body.
                "instance_id": getattr(e, "instance_id", None) or action.instance_id,
                "log_tail": getattr(e, "log_tail", None),
                # C4: set when the action gave up while the host was still
                # making progress (e.g. a cold-start pull).
                "recoverable": bool(getattr(e, "recoverable", False)),
            }

        # Update backoff state
        if action_succeeded and last_error is None:
            self._backoff_clear(intent.id)
        elif last_error is not None:
            self._backoff_record_failure(intent.id, spec_version=_spec_version(intent))

        # 4. Update status
        await self._update_status(
            intent,
            observed,
            last_error=last_error,
            # No replica needed replacing, so nothing is left to roll out for
            # a pending spec change (S-044).
            spec_settled=_spec_settled(observed, actions),
            drift_replace=drift_replace,
        )

    # ── S-042: Strategy helpers ──────────────────────────────────

    def _get_strategy_progress(self, intent: Any) -> dict[str, Any] | None:
        """Extract strategy_progress from intent status, if present."""
        try:
            sp = intent.status.strategy_progress
            if sp is None:
                return None
            if hasattr(sp, "model_dump"):
                return sp.model_dump()
            if isinstance(sp, dict):
                return sp
            return None
        except (AttributeError, TypeError):
            return None

    def _maybe_initiate_strategy(
        self,
        intent: Any,
        observed: dict[str, Any],
        actions: list[Action],
    ) -> dict[str, Any] | None:
        """Check if a deployment strategy should be initiated.

        A strategy is needed when:
        - There are REPLACE actions (model_source or backend config drift)
        - The intent specifies a known strategy (rolling / immediate)

        The REPLACE actions name the replicas to replace, so the rollout
        covers an edited backend config (S-044) as well as a version change.

        Returns strategy_progress dict if initiated, None otherwise.
        """
        from app.services.strategies import initiate_strategy

        replaces = [a for a in actions if a.type == ActionType.REPLACE]
        if not replaces:
            return None

        managed = observed["managed_instances"]
        candidates = observed["candidates"]

        progress = initiate_strategy(
            intent=intent,
            managed_instances=managed,
            candidates=candidates,
            drifted_instance_ids=[a.instance_id for a in replaces if a.instance_id],
        )
        return progress

    async def _continue_strategy(
        self,
        intent: Any,
        observed: dict[str, Any],
        strategy_progress: dict[str, Any],
    ) -> None:
        """Continue an in-flight deployment strategy.

        Drives the strategy state machine: computes the next action,
        executes it, and updates status with new progress.
        """
        from app.config import settings
        from app.services.strategies import continue_strategy, record_create_failure

        managed = observed["managed_instances"]
        candidates = observed["candidates"]
        gateway_aliases = observed["gateway_aliases"]

        # Compute health gate elapsed time from the current step start
        # (per §11.1 each replacement step has its own timeout window).
        # Fall back to overall strategy started_at for backward compat.
        step_started_at_str = strategy_progress.get(
            "step_started_at"
        ) or strategy_progress.get("started_at")
        health_gate_started_at = 0.0
        if step_started_at_str:
            try:
                step_started_dt = datetime.fromisoformat(step_started_at_str)
                health_gate_started_at = (
                    datetime.now(timezone.utc) - step_started_dt
                ).total_seconds()
            except (ValueError, TypeError):
                pass

        timeout = settings.reconcile_health_gate_timeout_s

        # If scale-down requested during strategy, defer it
        desired = intent.replicas
        if len(managed) > desired:
            logger.info(
                "Scale-down (%d→%d) deferred for intent %s while strategy active",
                len(managed),
                desired,
                intent.id,
            )

        action_dict, new_progress = continue_strategy(
            progress_data=strategy_progress,
            intent=intent,
            managed_instances=managed,
            candidates=candidates,
            gateway_aliases=gateway_aliases,
            health_gate_started_at=health_gate_started_at,
            health_gate_timeout_s=timeout,
        )

        # Execute the action if one is returned
        last_error = None
        if action_dict and action_dict.get("type") not in ("wait", "noop"):
            action_type = action_dict["type"]
            try:
                action = Action(
                    type=(
                        ActionType.CREATE
                        if action_type == "create"
                        else (
                            ActionType.STOP
                            if action_type == "stop"
                            else ActionType.NOOP
                        )
                    ),
                    intent_id=intent.id,
                    alias=intent.alias,
                    host_id=action_dict.get("host_id"),
                    instance_id=action_dict.get("instance_id"),
                    reason=action_dict.get("reason", ""),
                    priority=20 if action_type == "create" else 0,
                )
                result = await self._act(intent, action)

                # If create succeeded, capture instance_id for progress
                if action_type == "create" and result and new_progress:
                    created = result.get("instance", result)
                    new_progress["current_instance_id"] = created.get(
                        "id"
                    ) or created.get("instance_id")
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "Strategy action %s failed for intent %s: %s",
                    action_type,
                    intent.id,
                    e,
                )
                last_error = {
                    "code": type(e).__name__,
                    "message": str(e),
                    "host_id": action_dict.get("host_id"),
                    "source_uri": intent.model_source,
                    "at": datetime.now(timezone.utc).isoformat(),
                    "instance_id": getattr(e, "instance_id", None)
                    or action_dict.get("instance_id"),
                    "log_tail": getattr(e, "log_tail", None),
                    "recoverable": bool(getattr(e, "recoverable", False)),
                }
                if new_progress and isinstance(e, StartOutcomeUnknown):
                    # The replacement may be coming up. Hold the step where it
                    # is: the next tick adopts the instance if it exists, and
                    # moving to another host now could leave two.
                    new_progress["failed"] = new_progress.get("failed", 0) + 1
                elif new_progress and action_type == "create":
                    new_progress = record_create_failure(
                        progress_data=new_progress,
                        host_id=action_dict.get("host_id"),
                        candidate_host_ids=[h.id for h, _ in candidates],
                        message=f"Replacement failed on {action_dict.get('host_id')}",
                    )
                elif new_progress:
                    new_progress["failed"] = new_progress.get("failed", 0) + 1
                    failed_hosts = list(new_progress.get("failed_hosts", []))
                    if action_dict.get("host_id"):
                        failed_hosts.append(action_dict["host_id"])
                    new_progress["failed_hosts"] = failed_hosts

        # If strategy reached FAILED state, record backoff so retries
        # are paced per §11.5 ("Reconciler retries with backoff").
        if new_progress and new_progress.get("phase") == "failed":
            self._backoff_record_failure(intent.id, spec_version=_spec_version(intent))

        # Update status with strategy progress
        await self._update_status(
            intent,
            observed,
            last_error=last_error,
            strategy_progress=new_progress,
        )

    # ── Observe ────────────────────────────────────────────────

    async def _observe(self, intent: Any) -> dict[str, Any]:
        """Collect observed state for *intent*.

        Returns a dict with:
            managed_instances: instances with managed_by='intent' and intent_id==intent.id
            alias_instances: ALL instances serving this alias (managed + manual)
            manual_conflicts: manual instances serving this alias
            hosts: list of Host models
            snapshots: dict[host_id, HostResourceSnapshot]
            gateway_aliases: set of aliases registered in gateway
            candidates: list of (Host, snapshot) pairs from shared placement policy
            displaceable_map: dict[host_id, list[dict]] of displaceable instances
        """
        from app.database.hosts import host_db
        from app.redis_state import host_store, registry_store
        from app.routes.management.resources import _fetch_host_resource_snapshot
        from app.services.placement import find_candidates, find_displaceable_instances

        alias = intent.alias

        # 1. Fetch all hosts and resource snapshots
        hosts = await host_db.get_all_hosts()
        snapshots_list = await asyncio.gather(
            *[_fetch_host_resource_snapshot(h) for h in hosts]
        )
        snapshots: dict[str, Any] = {s.host_id: s for s in snapshots_list}

        # 2. Collect all instances for this alias across all hosts
        managed_instances: list[dict[str, Any]] = []
        alias_instances: list[dict[str, Any]] = []
        manual_conflicts: list[dict[str, Any]] = []
        # Orphaned instances (markers cleared while running, §12.4): the host
        # config still carries managed_by/intent_id, so exclude them here —
        # they are neither managed nor conflicts.
        try:
            r = redis_client()
            disowned: set[str] = {str(x) for x in await r.smembers(_DISOWNED_SET)}
        except Exception:  # noqa: BLE001
            disowned = set()
        seen_instance_ids: set[str] = set()
        for host in hosts:
            instances = await host_store.get_host_instances(host.id)
            for inst in instances:
                cfg = inst.get("config", inst)
                inst_alias = cfg.get("alias") or inst.get("alias")
                iid = inst.get("instance_id") or inst.get("id")
                if iid:
                    seen_instance_ids.add(iid)
                if iid and iid in disowned:
                    continue
                if inst_alias != alias:
                    continue
                # Annotate with host context
                inst["_host_id"] = host.id
                inst["_host_name"] = host.name
                alias_instances.append(inst)

                # Owned by this intent?
                managed_by = cfg.get("managed_by") or inst.get("managed_by")
                intent_id = cfg.get("intent_id") or inst.get("intent_id")
                if managed_by == "intent" and intent_id == intent.id:
                    managed_instances.append(inst)
                elif managed_by != "intent" or intent_id != intent.id:
                    # Manual instance or owned by a different intent
                    manual_conflicts.append(inst)

        # Prune disowned tombstones whose instance no longer exists anywhere.
        if disowned:
            stale = disowned - seen_instance_ids
            if stale:
                try:
                    r = redis_client()
                    await r.srem(_DISOWNED_SET, *stale)
                except Exception:
                    logger.warning("Could not prune disowned set", exc_info=True)

        # 2.5 Backend deep-compare inputs (S-044). The WS instance cache is
        # flat, so most backend fields never appear in it and a backend-only
        # edit would not read as drift. While a spec change is pending, load
        # each replica's real configuration so the comparison in _diff sees
        # every field. Bounded to that window: in steady state this would be
        # a host round-trip per replica per tick.
        spec_version = _spec_version(intent)
        if spec_version and managed_instances:
            from app.services.migration import capture_instance_config

            if self._config_cache_spec.get(intent.id) != spec_version:
                # New edit: the previous window's captures are stale.
                self._config_cache_spec[intent.id] = spec_version
                self._config_cache = {
                    k: v for k, v in self._config_cache.items() if k[0] != intent.id
                }

            hosts_by_id = {h.id: h for h in hosts}
            for inst in managed_instances:
                host = hosts_by_id.get(inst.get("_host_id"))
                iid = inst.get("instance_id") or inst.get("id")
                if host is None or not iid:
                    continue
                cache_key = (intent.id, iid, spec_version)
                if cache_key in self._config_cache:
                    inst["_full_config"] = self._config_cache[cache_key]
                    continue
                try:
                    full = await capture_instance_config(host, iid)
                except Exception:
                    # Without the real config there is nothing to compare the
                    # new spec against, and the flat cache carries almost none
                    # of the backend fields. Saying "no drift" here would
                    # declare the edit rolled out and drop it for good, so the
                    # replica is marked unknown and the edit stays pending.
                    logger.warning(
                        "Could not load full config for instance %s on %s; "
                        "the pending spec change stays unsettled",
                        iid,
                        host.name,
                        exc_info=True,
                    )
                    inst["_full_config_unknown"] = True
                    continue
                cfg = full.get("config", full)
                if isinstance(cfg, dict):
                    inst["_full_config"] = cfg
                    self._config_cache[cache_key] = cfg

        # 3. Gateway registry — which aliases are registered?
        gateway_aliases: set[str] = set()
        try:
            registry = await registry_store.get_registry()
            if isinstance(registry, dict):
                gateway_aliases = set(registry.keys())
        except Exception:
            logger.warning("Failed to read gateway registry", exc_info=True)

        # 4. Compute placement candidates using shared policy (§8.4)
        placement = intent.placement
        resources = intent.resources
        req_vram = (
            float(resources.vram_gb or 0) if hasattr(resources, "vram_gb") else 0.0
        )
        req_ram = (
            float(resources.ram_gb)
            if hasattr(resources, "ram_gb") and resources.ram_gb
            else None
        )
        req_roles = list(placement.roles) if placement.roles else ["inference"]
        req_gpu = (
            placement.gpu_type
            if hasattr(placement, "gpu_type") and placement.gpu_type
            else None
        )
        req_allow = (
            list(placement.host_allow)
            if hasattr(placement, "host_allow") and placement.host_allow
            else None
        )
        req_deny = (
            list(placement.host_deny)
            if hasattr(placement, "host_deny") and placement.host_deny
            else None
        )

        candidates = await find_candidates(
            hosts,
            snapshots,
            roles=req_roles,
            gpu_type=req_gpu,
            host_allow=req_allow,
            host_deny=req_deny,
            vram_gb=req_vram,
            ram_gb=req_ram,
            exclude_alias=alias,
        )

        # 5. Compute displaceable instances per host (for displacement evaluation)
        displaceable_map: dict[str, list[dict[str, Any]]] = {}
        intent_priority = intent.priority
        candidate_host_ids = {h.id for h, _ in candidates}
        # Pre-collect hosts with active training jobs (non-displaceable per §8.5)
        hosts_with_active_jobs: set[str] = set()
        try:
            from app.database.jobs import job_db
            from app.models.job import JobStatus

            for host in hosts:
                jobs = await job_db.get_jobs_by_host(host.id)
                if any(
                    j.status in (JobStatus.PENDING, JobStatus.RUNNING) for j in jobs
                ):
                    hosts_with_active_jobs.add(host.id)
        except Exception:
            logger.warning(
                "Failed to query active training jobs for displacement pre-filter",
                exc_info=True,
            )
        for host in hosts:
            if host.id in candidate_host_ids or host.status != "online":
                continue
            # Skip hosts with active training jobs (§8.5)
            if host.id in hosts_with_active_jobs:
                continue
            # Check if host has right roles/GPU (basic pre-filter)
            host_roles = host.roles or []
            if not all(r in host_roles for r in req_roles):
                continue
            if req_gpu and host.gpu_type != req_gpu:
                continue
            displaced = await find_displaceable_instances(
                host.id, intent_priority, preserve_alias=alias
            )
            if displaced:
                now = time.monotonic()
                filtered: list[dict[str, Any]] = []
                for d in displaced:
                    iid = d.get("instance_id") or d.get("id")
                    if iid is None or now >= self._displace_cooldown.get(iid, 0.0):
                        filtered.append(d)
                displaced = filtered
            if displaced:
                displaceable_map[host.id] = displaced

        return {
            "managed_instances": managed_instances,
            "alias_instances": alias_instances,
            "manual_conflicts": manual_conflicts,
            "hosts": hosts,
            "snapshots": snapshots,
            "gateway_aliases": gateway_aliases,
            "candidates": candidates,
            "displaceable_map": displaceable_map,
        }

    # ── Diff ───────────────────────────────────────────────────

    def _diff(
        self,
        intent: Any,
        observed: dict[str, Any],
    ) -> list[Action]:
        """Compare desired vs observed state and produce actions.

        Implements deployment-intent.md §8.2 diff and actions table.
        Uses shared placement policy candidates and evaluates
        priority-aware displacement when capacity is insufficient.
        """
        desired = intent.replicas
        managed = observed["managed_instances"]
        is_orphan = _intent_orphan(intent)

        observed_count = len(managed)
        actions: list[Action] = []

        # ── Deleting intent ──────────────────────────────────────
        if _intent_phase(intent) == "deleting":
            for inst in managed:
                inst_id = inst.get("instance_id") or inst.get("id")
                if not inst_id:
                    continue
                if is_orphan:
                    actions.append(
                        Action(
                            type=ActionType.DISOWN,
                            intent_id=intent.id,
                            alias=intent.alias,
                            host_id=inst.get("_host_id"),
                            instance_id=inst_id,
                            reason="Intent deleted (orphan)",
                            priority=1,
                        )
                    )
                else:
                    actions.append(
                        Action(
                            type=ActionType.STOP,
                            intent_id=intent.id,
                            alias=intent.alias,
                            host_id=inst.get("_host_id"),
                            instance_id=inst_id,
                            reason="Intent deleted",
                            priority=0,
                        )
                    )
            return actions

        # ── replicas == 0 → stop all managed instances ───────────
        if desired == 0:
            for inst in managed:
                inst_id = inst.get("instance_id") or inst.get("id")
                if inst_id:
                    actions.append(
                        Action(
                            type=ActionType.STOP,
                            intent_id=intent.id,
                            alias=intent.alias,
                            host_id=inst.get("_host_id"),
                            instance_id=inst_id,
                            reason="replicas=0",
                            priority=0,
                        )
                    )
            return actions

        # ── Check for drift (model_source + backend config) ──────
        for inst in managed:
            cfg = inst.get("config", inst)
            inst_source = cfg.get("model_source") or inst.get("model_source")
            inst_id = inst.get("instance_id") or inst.get("id")
            has_source_drift = inst_source and inst_source != intent.model_source
            # _observe attaches the full config while a spec change is
            # pending; otherwise the flat cache entry is all there is.
            drifted_keys = _detect_backend_drift(
                intent, inst.get("_full_config") or cfg
            )
            has_backend_drift = bool(drifted_keys)

            if has_source_drift:
                actions.append(
                    Action(
                        type=ActionType.REPLACE,
                        intent_id=intent.id,
                        alias=intent.alias,
                        host_id=inst.get("_host_id"),
                        instance_id=inst_id,
                        reason=(
                            f"model_source drift: {inst_source} → {intent.model_source}"
                        ),
                        priority=20,
                    )
                )
            elif has_backend_drift and not has_source_drift:
                # Churn circuit breaker (C1): a drift vector the host keeps
                # re-producing would otherwise trap the intent in a
                # stop/recreate loop forever. After max_drift_replace_attempts
                # rounds the REPLACE is no longer planned; the intent records
                # a BackendDriftUnsettled error naming the mismatching keys
                # instead, and the spec change stays pending so the next edit
                # resets the counter.
                attempts = getattr(intent.status, "drift_replace_attempts", 0) or 0
                if attempts < settings.max_drift_replace_attempts:
                    actions.append(
                        Action(
                            type=ActionType.REPLACE,
                            intent_id=intent.id,
                            alias=intent.alias,
                            host_id=inst.get("_host_id"),
                            instance_id=inst_id,
                            reason="backend config drift",
                            priority=20,
                        )
                    )
                else:
                    logger.error(
                        "Intent %s (%s): backend drift unsettled after %d "
                        "REPLACE attempts (keys: %s); stopping the loop",
                        intent.id,
                        intent.alias,
                        attempts,
                        ", ".join(sorted(drifted_keys)),
                    )
                    observed["_drift_unsettled"] = drifted_keys

            # Managed instances in failed/stopped/error are drift (§8.2):
            # RECREATE restarts them; if the restart fails, the replica is
            # deleted and re-created with backoff (so no /stop spam — the
            # action restarts instead of stopping).
            status = inst.get("status") or inst.get("state", "")
            if status in ("failed", "stopped", "error") and not any(
                a.instance_id == inst_id and a.type == ActionType.REPLACE
                for a in actions
            ):
                actions.append(
                    Action(
                        type=ActionType.RECREATE,
                        intent_id=intent.id,
                        alias=intent.alias,
                        host_id=inst.get("_host_id"),
                        instance_id=inst_id,
                        reason=f"Instance {status}, recreating",
                        priority=15,
                    )
                )

        # ── Observed < Desired → CREATE (placement policy) ───────
        shortfall = desired - observed_count
        if shortfall > 0:
            candidates = observed.get("candidates", [])
            for i in range(min(shortfall, len(candidates))):
                host, _snap = candidates[i]
                actions.append(
                    Action(
                        type=ActionType.CREATE,
                        intent_id=intent.id,
                        alias=intent.alias,
                        host_id=host.id,
                        host_name=host.name,
                        reason=f"shortfall {i + 1}/{shortfall}",
                        priority=50,
                    )
                )

            # If still short, evaluate priority-aware displacement (§8.5)
            remaining = shortfall - min(shortfall, len(candidates))
            if remaining > 0:
                displaceable_map = observed.get("displaceable_map", {})
                for host_id, displaceable_list in displaceable_map.items():
                    if remaining <= 0:
                        break
                    if not displaceable_list:
                        continue
                    inst = displaceable_list[0]
                    inst_id = inst.get("instance_id") or inst.get("id", "")
                    inst_alias = inst.get("config", inst).get("alias") or inst.get(
                        "alias", ""
                    )
                    if not inst_id:
                        continue
                    actions.append(
                        Action(
                            type=ActionType.MIGRATE,
                            intent_id=intent.id,
                            alias=inst_alias,
                            host_id=host_id,
                            instance_id=inst_id,
                            reason=(
                                f"Displacing {inst_alias} "
                                f"({inst.get('_priority', '?')}) "
                                f"to free capacity for {intent.alias} "
                                f"({intent.priority})"
                            ),
                            priority=25,
                        )
                    )
                    remaining -= 1

        draining_host_ids = {
            h.id for h in observed.get("hosts", []) if h.drain_state is not None
        }

        # ── Observed > Desired → STOP surplus ────────────────────
        surplus = observed_count - desired
        if surplus > 0:
            # Sort per §8.2: unhealthy/failed first (oldest→newest within
            # that group), then healthy instances most-recently-created
            # first so long-lived replicas survive. Tiebreak by host load
            # (fewest running instances stopped first).
            snapshots = observed.get("snapshots", {})

            def _host_load(inst: dict[str, Any]) -> int:
                snap = snapshots.get(inst.get("_host_id"))
                return getattr(snap, "running_instance_count", 0) or 0

            unhealthy_insts: list[dict[str, Any]] = []
            healthy_insts: list[dict[str, Any]] = []
            for inst in managed:
                status = inst.get("status") or inst.get("state", "")
                if status in ("failed", "stopped", "error"):
                    unhealthy_insts.append(inst)
                else:
                    healthy_insts.append(inst)
            unhealthy_insts.sort(
                key=lambda i: ((i.get("created_at") or "0"), _host_load(i))
            )
            # Newest-first primary, least-loaded secondary. Two-pass stable
            # sort: a single reverse=True would also flip the load tiebreak.
            healthy_insts.sort(key=_host_load)
            healthy_insts.sort(key=lambda i: i.get("created_at") or "0", reverse=True)
            # A replica on a draining host is the best one to give up: the
            # host is being emptied anyway, so removing it as surplus saves a
            # migration (S-043 §4.2). Stable, so it only reorders ties.
            if draining_host_ids:
                for group in (unhealthy_insts, healthy_insts):
                    group.sort(key=lambda i: i.get("_host_id") not in draining_host_ids)
            to_stop = (unhealthy_insts + healthy_insts)[:surplus]
            for inst in to_stop:
                inst_id = inst.get("instance_id") or inst.get("id")
                if inst_id:
                    actions.append(
                        Action(
                            type=ActionType.STOP,
                            intent_id=intent.id,
                            alias=intent.alias,
                            host_id=inst.get("_host_id"),
                            instance_id=inst_id,
                            reason="surplus replica",
                            priority=0,
                        )
                    )

        # ── Draining hosts → evacuate managed replicas (S-043 §4.2) ──
        if draining_host_ids:
            actions = self._apply_drain_actions(
                intent, observed, actions, draining_host_ids
            )

        # Sort by priority so stops/disowns execute before migrates/creates
        actions.sort(key=lambda a: a.priority)
        return actions

    def _no_evacuation_target_reason(self, intent: Any) -> str:
        """Explain what a host would have to satisfy to accept this replica."""
        placement = intent.placement
        roles = list(placement.roles) if placement.roles else ["inference"]
        parts = [f"roles {roles}"]
        gpu_type = getattr(placement, "gpu_type", None)
        if gpu_type:
            parts.append(f"gpu_type '{gpu_type}'")
        vram_gb = getattr(intent.resources, "vram_gb", None)
        if vram_gb:
            parts.append(f"vram >= {vram_gb} GB")
        host_allow = getattr(placement, "host_allow", None)
        if host_allow:
            parts.append(f"host in allow-list {list(host_allow)}")
        parts.append(f"no existing replica of '{intent.alias}'")
        return "No eligible host: needs " + ", ".join(parts)

    def _apply_drain_actions(
        self,
        intent: Any,
        observed: dict[str, Any],
        actions: list[Action],
        draining_host_ids: set[str],
    ) -> list[Action]:
        """Add evacuation actions for managed replicas on draining hosts.

        Implements host-draining.md §4.2:

        - A running replica is migrated to the best remaining placement
          candidate. With no candidate the action is still emitted (with no
          target) so the stall is recorded and reported rather than silently
          skipped — the replica keeps serving either way.
        - A replica that is not running would otherwise be RECREATE'd on the
          same host, which fights the drain; it is stopped and deleted
          instead so the next CREATE places it elsewhere.
        - A replica already being replaced for drift is left to that path:
          the rollout places its replacement through placement, which
          excludes the draining host, so the drain progresses anyway.
        """
        candidates = observed.get("candidates", [])
        target = candidates[0][0] if candidates else None

        result = list(actions)
        for inst in observed["managed_instances"]:
            host_id = inst.get("_host_id")
            inst_id = inst.get("instance_id") or inst.get("id")
            if not inst_id or host_id not in draining_host_ids:
                continue

            existing = [a for a in result if a.instance_id == inst_id]
            if any(
                a.type in (ActionType.REPLACE, ActionType.STOP, ActionType.DISOWN)
                for a in existing
            ):
                continue

            status = inst.get("status") or inst.get("state", "")
            if status in ("failed", "stopped", "error"):
                result = [
                    a
                    for a in result
                    if a.type != ActionType.RECREATE or a.instance_id != inst_id
                ]
                result.append(
                    Action(
                        type=ActionType.STOP,
                        intent_id=intent.id,
                        alias=intent.alias,
                        host_id=host_id,
                        instance_id=inst_id,
                        reason=f"host draining, replica is {status}",
                        priority=0,
                    )
                )
                continue

            if any(a.type == ActionType.EVACUATE for a in existing):
                continue

            result.append(
                Action(
                    type=ActionType.EVACUATE,
                    intent_id=intent.id,
                    alias=intent.alias,
                    host_id=host_id,
                    host_name=inst.get("_host_name"),
                    instance_id=inst_id,
                    target_host_id=target.id if target else None,
                    target_host_name=target.name if target else None,
                    reason=(
                        f"host draining → migrate to {target.name}"
                        if target
                        else self._no_evacuation_target_reason(intent)
                    ),
                    # Last: evacuation must never starve the intent's own
                    # convergence, and a stalled drain retries every tick.
                    priority=60,
                )
            )

        return result

    # ── Act ────────────────────────────────────────────────────

    async def _act(
        self,
        intent: Any,
        action: Action,
    ) -> dict[str, Any] | None:
        """Execute one reconciliation action via Solar Host primitives.

        Returns the created instance dict on create, None otherwise.
        """
        from app.database.hosts import host_db
        from app.services.migration import (
            create_instance_on_host,
            stop_source_instance,
        )

        if action.type == ActionType.NOOP:
            return None

        if action.type == ActionType.STOP:
            if not action.host_id or not action.instance_id:
                return None
            host = await host_db.get_host(action.host_id)
            if host is None:
                logger.warning("Host %s not found for stop action", action.host_id)
                return None
            logger.info(
                "Stopping instance %s on %s (reason: %s)",
                action.instance_id,
                host.name,
                action.reason,
            )
            await stop_source_instance(host, action.instance_id)
            # Delete the instance so the reconciler stops observing it.
            # Without this, stopped instances persist and observed_replicas
            # can never reach 0 for DELETE / scale-to-zero flows.
            try:
                await self._delete_instance(host, action.instance_id)
            except Exception:
                logger.warning(
                    "Failed to delete instance %s on %s after stop",
                    action.instance_id,
                    host.name,
                    exc_info=True,
                )
            return None

        if action.type == ActionType.DISOWN:
            # Clear ownership markers from the instance in Redis so the
            # reconciler stops tracking it.  The underlying Solar Host
            # instance config retains the markers (there is no host-side
            # PATCH for running instances), but the stale reference is
            # harmless: the intent is being deleted and will not be
            # reconciled again.
            if not action.host_id or not action.instance_id:
                return None
            from app.redis_state import host_store as _hs

            instances = await _hs.get_host_instances(action.host_id)
            found = False
            for inst in instances:
                iid = inst.get("instance_id") or inst.get("id")
                if iid == action.instance_id:
                    cfg = inst.get("config", inst)
                    if isinstance(cfg, dict):
                        cfg.pop("managed_by", None)
                        cfg.pop("intent_id", None)
                        # Flat WS cache entries have no nested "config"; the
                        # fallback makes cfg == inst, so assigning it back
                        # would create a self-referential dict that json
                        # serialization rejects ("Circular reference
                        # detected"). Only re-attach real nested configs.
                        if cfg is not inst:
                            inst["config"] = cfg
                    inst.pop("managed_by", None)
                    inst.pop("intent_id", None)
                    found = True
                    break
            if found:
                await _hs.set_host_instances(action.host_id, instances)
                # Tombstone the instance so a cache re-seed (host
                # registration/instances_update after a control restart)
                # cannot make the reconciler treat the orphan as managed
                # again — the host config retains the markers by design
                # (§12.4 + §5.2 restart-safe recompute).
                try:
                    r = redis_client()
                    await r.sadd(_DISOWNED_SET, action.instance_id)
                except Exception:
                    logger.warning(
                        "Could not record disowned instance %s",
                        action.instance_id,
                        exc_info=True,
                    )
            logger.info(
                "Disowned instance %s on host %s (reason: %s)",
                action.instance_id,
                action.host_id,
                action.reason,
            )
            return None

        if action.type == ActionType.CREATE:
            if not action.host_id:
                return None
            host = await host_db.get_host(action.host_id)
            if host is None:
                logger.warning("Host %s not found for create action", action.host_id)
                return None

            instance_config = self._build_instance_config(intent, host)

            # Hold the intent's estimated VRAM on this host while the pull +
            # start run: without a running instance yet, placement would
            # otherwise see the host as free and oversubscribe it.
            await self._reserve_cold_start(intent, action.host_id)

            logger.info(
                "Creating instance for alias=%s on %s (reason: %s)",
                intent.alias,
                host.name,
                action.reason,
            )
            result = await create_instance_on_host(host, instance_config)
            # The host wraps the response in {"instance": {...}};
            # extract the instance and start it (host creates in stopped state).
            created = result.get("instance", result)
            instance_id = created.get("id") or created.get("instance_id")
            if instance_id:
                logger.info("Starting instance %s on %s", instance_id, host.name)
                try:
                    await self._start_instance(host, instance_id)
                except StartOutcomeUnknown:
                    logger.warning(
                        "Start outcome unknown for new instance %s on %s; "
                        "leaving it in place",
                        instance_id,
                        host.name,
                    )
                    raise
                except Exception:
                    # A replica the host refused to start is not a replica.
                    # Leaving it behind would pile up one dead instance per
                    # retry on the host, and each would still count as an
                    # observed replica of this intent.
                    logger.warning(
                        "Instance %s failed to start on %s; removing it",
                        instance_id,
                        host.name,
                    )
                    await self._delete_instance(host, instance_id)
                    raise
            return result

        if action.type == ActionType.REPLACE:
            # Replace = retire the drifted replica; the next tick's CREATE
            # places its replacement per placement policy. Reached only when
            # no strategy took the rollout over (deployment-intent.md §11).
            #
            # Retiring means stop *and* delete: a stopped replica still counts
            # towards observed_replicas, so leaving it would hold the intent at
            # its desired count with nothing serving — no CREATE for the
            # replacement, and a RECREATE suppressed by this very REPLACE.
            if action.instance_id and action.host_id:
                host = await host_db.get_host(action.host_id)
                if host:
                    logger.info(
                        "Retiring drifted instance %s on %s for replacement",
                        action.instance_id,
                        host.name,
                    )
                    await stop_source_instance(host, action.instance_id)
                    await self._delete_instance(host, action.instance_id)
            return None

        if action.type == ActionType.RECREATE:
            # §8.2: "restart or recreate on the same or a new host (with
            # backoff)". Try restart in place first; if the instance
            # cannot be restarted, delete the broken replica so
            # observed_count drops and the next tick's CREATE places a
            # fresh replica (same or new host per placement). Raising
            # records exponential backoff via _reconcile_one.
            if action.instance_id and action.host_id:
                host = await host_db.get_host(action.host_id)
                if host:
                    logger.info(
                        "Restarting instance %s on %s for recreation",
                        action.instance_id,
                        host.name,
                    )
                    try:
                        await self._start_instance(host, action.instance_id)
                        return None
                    except StartOutcomeUnknown:
                        # The replica may be starting right now; the next
                        # observation decides. Only the backoff is recorded.
                        logger.warning(
                            "Restart outcome unknown for instance %s on %s; "
                            "leaving it in place",
                            action.instance_id,
                            host.name,
                        )
                        raise
                    except HTTPException:
                        logger.warning(
                            "Restart failed for instance %s on %s, recreating",
                            action.instance_id,
                            host.name,
                            exc_info=True,
                        )
                        try:
                            await self._delete_instance(host, action.instance_id)
                        except Exception:
                            logger.warning(
                                "Failed to delete instance %s on %s after failed restart",
                                action.instance_id,
                                host.name,
                                exc_info=True,
                            )
                        raise  # record backoff so retries are paced
            return None

        if action.type == ActionType.EVACUATE:
            # Evacuate = move this intent's own replica off a draining host
            # (S-043 §4.2). The target was chosen in _diff from the intent's
            # placement candidates, so it already satisfies roles, GPU type,
            # allow/deny, resources and one-replica-per-host.
            if not action.instance_id or not action.host_id:
                return None
            from app.services import drain as drain_service
            from app.services.migration import execute_migration

            if not action.target_host_id:
                # Nowhere to go: the replica keeps serving and the drain stays
                # unfinished (§4.3). Recorded, not raised — a stall is not a
                # failure, and backing off would only slow down the retry that
                # succeeds once capacity appears.
                await drain_service.record_stall(
                    action.host_id, action.instance_id, action.reason
                )
                logger.warning(
                    "Cannot evacuate instance %s (%s) from draining host %s: %s",
                    action.instance_id,
                    action.alias,
                    action.host_id,
                    action.reason,
                )
                return None

            logger.info(
                "Evacuating instance %s (%s) from draining host %s to %s",
                action.instance_id,
                action.alias,
                action.host_name or action.host_id,
                action.target_host_name or action.target_host_id,
            )
            # Hold the intent's estimated VRAM on the target while the
            # migration's pull + start run there.
            if action.target_host_id:
                await self._reserve_cold_start(intent, action.target_host_id)
            # Leave this intent alone while the migration disowns the source
            # and brings the target up — otherwise the next diff sees neither
            # and races a duplicate CREATE.
            self._settle_until[intent.id] = time.monotonic() + _MIGRATE_SETTLE_S
            try:
                result = await execute_migration(
                    instance_id=action.instance_id,
                    source_host_id=action.host_id,
                    target_host_id=action.target_host_id,
                    # An operator's drain request is the explicit policy
                    # decision the S-037 production safeguard asks for.
                    allow_production=True,
                )
            except Exception:
                logger.exception(
                    "Evacuation failed for instance %s: %s → %s",
                    action.instance_id,
                    action.host_id,
                    action.target_host_id,
                )
                raise
            if result.status != "completed":
                message = result.error or "migration did not complete"
                await drain_service.record_stall(
                    action.host_id, action.instance_id, f"Migration failed: {message}"
                )
                raise RuntimeError(f"Evacuation failed: {message}")
            await drain_service.clear_stall(action.host_id, action.instance_id)

            # S-037 leaves the source stopped and disowned, which is right for
            # an operator's one-off move but wrong for a drain: the leftover
            # still serves the alias in the host's instance list, so placement
            # excludes this host for the intent from then on (exclude_alias).
            # The replica could never come back, a later drain of its new host
            # would stall with "no existing replica of <alias>", and the intent
            # would carry a permanent ManualInstanceConflict condition. The
            # drain contract is that the host ends up empty, so remove it.
            source_host = await host_db.get_host(action.host_id)
            if source_host is not None:
                await self._delete_instance(source_host, action.instance_id)

            return {"migration_id": result.migration_id, "status": result.status}

        if action.type == ActionType.MIGRATE:
            # Migrate = use S-037 to move instance off this host, freeing capacity
            if not action.instance_id or not action.host_id:
                return None
            from app.routes.management.resources import _fetch_host_resource_snapshot
            from app.services.migration import execute_migration
            from app.services.placement import find_candidates

            # Look up the source host to inherit its GPU type and roles as
            # placement constraints for the migration target (§8.5: move to
            # "another eligible host" implies matching capabilities).
            source_host = await host_db.get_host(action.host_id)
            host_roles = (
                source_host.roles
                if source_host and source_host.roles
                else ["inference"]
            )
            host_gpu = source_host.gpu_type if source_host else None

            # Select a target host using placement policy
            all_hosts = await host_db.get_all_hosts()
            snapshots_list = await asyncio.gather(
                *[_fetch_host_resource_snapshot(h) for h in all_hosts]
            )
            snapshots_map = {s.host_id: s for s in snapshots_list}

            target_candidates = await find_candidates(
                all_hosts,
                snapshots_map,
                roles=host_roles,
                gpu_type=host_gpu,
                vram_gb=0.0,
                exclude_alias=action.alias,
            )
            # Exclude the source host from candidates
            target_candidates = [
                (h, s) for h, s in target_candidates if h.id != action.host_id
            ]

            if not target_candidates:
                # §8.5: "Only stop it if no migration target exists and
                # policy allows (e.g. ephemeral)." staging is migrated,
                # not stopped, when possible — leave it untouched.
                from app.redis_state import host_store as _hs

                (
                    inst_priority,
                    displaced_intent_id,
                ) = await self._displaced_instance_info(
                    action.host_id, action.instance_id
                )
                if displaced_intent_id:
                    self._settle_until[displaced_intent_id] = (
                        time.monotonic() + _MIGRATE_SETTLE_S
                    )
                # No target: don't re-attempt this displacement every tick
                # (it cannot succeed while the cluster lacks an eligible
                # host — re-trying only starves the CREATE actions).
                self._displace_cooldown[action.instance_id] = (
                    time.monotonic() + _DISPLACE_COOLDOWN_S
                )
                if inst_priority == "ephemeral":
                    if source_host is None:
                        logger.warning(
                            "Host %s not found for stop fallback of instance %s",
                            action.host_id,
                            action.instance_id,
                        )
                        return None
                    logger.info(
                        "No migration target for %s (%s); ephemeral → stop",
                        action.instance_id,
                        action.alias,
                    )
                    await stop_source_instance(source_host, action.instance_id)
                    try:
                        await self._delete_instance(source_host, action.instance_id)
                    except Exception:
                        logger.warning(
                            "Failed to delete instance %s on %s after stop",
                            action.instance_id,
                            source_host.name,
                            exc_info=True,
                        )
                    return None
                logger.warning(
                    "No migration target found for instance %s (alias=%s, "
                    "priority=%s); leaving in place",
                    action.instance_id,
                    action.alias,
                    inst_priority,
                )
                return None

            target_host, _tsnap = target_candidates[0]
            logger.info(
                "Migrating instance %s (%s) from %s to %s (reason: %s)",
                action.instance_id,
                action.alias,
                action.host_id,
                target_host.name,
                action.reason,
            )
            # Leave the displaced intent alone while the migration moves its
            # instance — otherwise its own diff sees the disowned source and
            # races a duplicate CREATE against the target being placed.
            _, displaced_intent_id = await self._displaced_instance_info(
                action.host_id, action.instance_id
            )
            if displaced_intent_id:
                self._settle_until[displaced_intent_id] = (
                    time.monotonic() + _MIGRATE_SETTLE_S
                )
            if displaced_intent_id:
                # Hold capacity on the target while the migration's pull +
                # start runs. Keyed by the DISPLACED intent — the instance
                # arriving there is its replica, and its own sweep releases
                # the reservation once the instance is observed running.
                from app.database.intents import intent_db

                displaced = await intent_db.get_intent(displaced_intent_id)
                if displaced is not None:
                    await self._reserve_cold_start(displaced, target_host.id)
            try:
                result = await execute_migration(
                    instance_id=action.instance_id,
                    source_host_id=action.host_id,
                    target_host_id=target_host.id,
                    allow_production=False,
                )
                # The source stays visible in the cache until the host's WS
                # push lands; without a cooldown the next tick re-migrates
                # the same instance (disown already cleared its markers, so
                # the settle lookup finds no intent to protect).
                self._displace_cooldown[action.instance_id] = (
                    time.monotonic() + _DISPLACE_COOLDOWN_S
                )
                return {"migration_id": result.migration_id, "status": result.status}
            except Exception:
                logger.exception(
                    "Migration failed for instance %s: %s → %s",
                    action.instance_id,
                    action.host_id,
                    target_host.id,
                )
                raise

        return None

    # ── Start / Delete instance helpers ────────────────────────

    async def _displaced_instance_info(
        self, host_id: str, instance_id: str
    ) -> tuple[str, str | None]:
        """Return ``(priority, intent_id)`` of the displaced instance.

        Reads the reconciler's own Redis cache view (flat or nested). The
        displaced workload's ``intent_id`` lets the reconciler leave that
        intent alone while the migration moves/removes its instance (§8.5).
        """
        from app.redis_state import host_store as _hs

        priority = "production"
        intent_id: str | None = None
        try:
            insts = await _hs.get_host_instances(host_id)
            for inst in insts:
                iid = inst.get("instance_id") or inst.get("id")
                if iid == instance_id:
                    priority = (
                        inst.get("priority")
                        or inst.get("config", {}).get("priority")
                        or "production"
                    )
                    intent_id = inst.get("intent_id") or inst.get("config", {}).get(
                        "intent_id"
                    )
                    break
        except Exception:
            logger.warning(
                "Could not read priority for instance %s", instance_id, exc_info=True
            )
        return priority, intent_id

    # ── Cold-start reservations ─────────────────────────────────

    async def _reserve_cold_start(self, intent: Any, host_id: str) -> None:
        """Reserve the intent's estimated VRAM on *host_id* before a cold start.

        The host has no running instance while the model downloads/loads,
        so without a reservation placement would treat it as free and could
        oversubscribe it. Idempotent per ``(intent, host)``: a retry after a
        timeout finds the existing reservation and does not stack a second
        one. The host's capacity check doubles as an early gate — a 409
        aborts the action before any download starts.
        """
        from app.database.hosts import host_db
        from app.services.reservation import (
            get_reconcile_reservations,
            reserve_host_capacity,
            store_reconcile_reservation,
        )

        resources = intent.resources
        vram_gb = float(getattr(resources, "vram_gb", 0) or 0)
        ram_gb = float(getattr(resources, "ram_gb", 0) or 0)
        if vram_gb <= 0:
            return

        existing = await get_reconcile_reservations(intent.id)
        if host_id in existing:
            return

        host = await host_db.get_host(host_id)
        if host is None:
            logger.warning("Host %s not found for cold-start reservation", host_id)
            return

        ttl = settings.model_pull_timeout_s + settings.host_start_timeout_s + 600
        try:
            host_reservation_id = await reserve_host_capacity(
                host,
                vram_gb=vram_gb,
                ram_gb=ram_gb if ram_gb > 0 else None,
                ttl_seconds=ttl,
                requester=f"intent:{intent.alias}",
                job_id=f"intent:{intent.id}",
            )
        except HTTPException:
            logger.warning(
                "Cold-start reservation failed for intent %s on host %s — "
                "aborting action before download",
                intent.id,
                host_id,
                exc_info=True,
            )
            raise
        await store_reconcile_reservation(
            intent.id, host_id, host_reservation_id, vram_gb, ram_gb
        )
        logger.info(
            "Cold-start reservation %s for intent %s on host %s "
            "(vram=%.1f GB, ram=%.1f GB)",
            host_reservation_id,
            intent.id,
            host_id,
            vram_gb,
            ram_gb,
        )

    async def _release_finished_reservations(self, intent: Any) -> None:
        """Release cold-start reservations whose instance is running or gone.

        A reservation is held from action start until the instance it
        protects is observed ``running`` (the host then reports its real
        VRAM usage, so holding further would double-count) or no longer
        exists on the host (definitive failure, delete, or scale-to-zero).
        An instance still starting/stopped/failed keeps its reservation —
        the recreate/restart it is heading for needs the capacity — and the
        host-side TTL reaper is the backstop for a crashed control plane.
        """
        from app.database.hosts import host_db
        from app.redis_state import host_store as _hs
        from app.services.reservation import (
            get_reconcile_reservations,
            release_host_capacity,
            remove_reconcile_reservation,
        )

        try:
            reservations = await get_reconcile_reservations(intent.id)
        except Exception:
            logger.warning(
                "Could not read reservations for intent %s",
                intent.id,
                exc_info=True,
            )
            return
        if not reservations:
            return

        for host_id, data in reservations.items():
            try:
                instances = await _hs.get_host_instances(host_id)
            except Exception:
                logger.warning(
                    "Could not read instances for host %s", host_id, exc_info=True
                )
                continue
            inst = next(
                (
                    i
                    for i in instances
                    if (i.get("intent_id") or i.get("config", {}).get("intent_id"))
                    == intent.id
                ),
                None,
            )
            if (
                inst is not None
                and (inst.get("status") or inst.get("state", "")) != "running"
            ):
                continue  # still starting / stopped / failed — keep holding

            host = await host_db.get_host(host_id)
            host_reservation_id = data.get("host_reservation_id") or ""
            if host is not None and host_reservation_id:
                try:
                    await release_host_capacity(host, host_reservation_id)
                except HTTPException as exc:
                    logger.warning(
                        "Failed to release reservation %s for intent %s on %s: %s",
                        host_reservation_id,
                        intent.id,
                        host_id,
                        exc.detail,
                    )
            await remove_reconcile_reservation(intent.id, host_id)
            logger.info(
                "Released cold-start reservation for intent %s on host %s",
                intent.id,
                host_id,
            )

    async def _start_instance(self, host: Any, instance_id: str) -> None:
        """Start a stopped instance on *host* via POST /instances/{id}/start.

        Raises HTTPException on failure so the reconciler records last_error.
        """
        import aiohttp

        from app.config import settings

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{host.url.rstrip('/')}/instances/{instance_id}/start"
                headers = {"X-API-Key": host.api_key}
                async with session.post(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=settings.host_start_timeout_s),
                ) as resp:
                    if resp.status == 200:
                        return
                    text = await resp.text()
                    # The host answers start failures with a structured body
                    # (C2): {"detail", "instance_id", "exit_code", "log_tail"}.
                    # Parse what we can so last_error links the failure to
                    # its process logs; older hosts send a plain string.
                    (
                        detail_msg,
                        body_instance_id,
                        exit_code,
                        log_tail,
                    ) = _parse_start_failure_body(text)
                    raise InstanceStartFailed(
                        detail=(
                            f"Host '{host.name}' failed to start instance "
                            f"{body_instance_id or instance_id}: HTTP "
                            f"{resp.status} — {detail_msg}"
                        ),
                        instance_id=body_instance_id or instance_id,
                        exit_code=exit_code,
                        log_tail=log_tail,
                    )
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise StartOutcomeUnknown(
                f"Host '{host.name}' did not answer the start call for "
                f"{instance_id}: {e}"
            )

    async def _delete_instance(self, host: Any, instance_id: str) -> None:
        """Delete an instance from *host* via DELETE /instances/{id}."""
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{host.url.rstrip('/')}/instances/{instance_id}"
                headers = {"X-API-Key": host.api_key}
                async with session.delete(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status not in (200, 204, 404):
                        text = await resp.text()
                        logger.warning(
                            "Failed to delete instance %s on %s: HTTP %s %s",
                            instance_id,
                            host.name,
                            resp.status,
                            text,
                        )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Failed to delete instance %s on %s: %s",
                instance_id,
                host.name,
                e,
            )

    # ── Build instance config ──────────────────────────────────

    def _build_instance_config(self, intent: Any, host: Any) -> dict[str, Any]:
        """Compose a Solar Host InstanceConfig from the intent.

        Implements deployment-intent.md §6 mapping: alias, model_source,
        priority, managed_by, intent_id, plus backend runtime params.

        The host expects ``managed_by``, ``intent_id``, and ``priority``
        at the TOP LEVEL (outside ``config``), matching the Instance model.
        """
        config: dict[str, Any] = {
            "backend_type": intent.backend.get("backend_type", "llamacpp"),
            "alias": intent.alias,
            "model_source": intent.model_source,
        }

        # Copy backend runtime params, excluding backend_type (already set)
        for key, value in intent.backend.items():
            if key == "backend_type":
                continue
            config[key] = value

        return {
            "config": config,
            "managed_by": "intent",
            "intent_id": intent.id,
            "priority": intent.priority,
        }

    # ── Update status ──────────────────────────────────────────

    async def _update_status(
        self,
        intent: Any,
        observed: dict[str, Any],
        last_error: dict[str, Any] | None = None,
        strategy_progress: dict[str, Any] | None = None,
        spec_settled: bool = False,
        drift_replace: bool = False,
    ) -> None:
        """Compute and persist the intent status after reconciliation.

        Implements deployment-intent.md §10.2 status fields.
        When *strategy_progress* is provided (S-042), it is persisted
        into the status JSON so strategy state survives restarts.

        *spec_settled* clears the ``spec_changed_at`` marker: the pass found
        no replica drifting from the spec, so the deep-compare window from
        S-044 has served its purpose. Status JSON is rebuilt from scratch on
        every write, so the marker has to be carried forward explicitly.

        *drift_replace* reports whether the pass planned a drift-driven
        REPLACE; it feeds the C1 churn circuit breaker counter
        (``drift_replace_attempts``), which is reset when the spec settles
        or is edited.
        """
        from app.database.intents import intent_db
        from app.models.intent import (
            Condition,
            IntentPhase,
            LastError,
            ReconcileState,
            ReplicaEntry,
        )

        managed = observed["managed_instances"]
        gateway_aliases = observed["gateway_aliases"]

        observed_count = len(managed)
        desired = intent.replicas
        alias = intent.alias

        # Count ready replicas (running AND in gateway registry)
        ready_count = 0
        updated_count = 0
        replica_set: list[dict[str, Any]] = []

        for inst in managed:
            cfg = inst.get("config", inst)
            status = inst.get("status") or inst.get("state", "")
            inst_id = inst.get("instance_id") or inst.get("id")
            inst_source = cfg.get("model_source") or inst.get("model_source")
            healthy = status == "running" and alias in gateway_aliases
            on_target_source = inst_source == intent.model_source

            if healthy:
                ready_count += 1
                if on_target_source:
                    updated_count += 1

            replica_set.append(
                ReplicaEntry(
                    host_id=inst.get("_host_id"),
                    host_name=inst.get("_host_name"),
                    instance_id=inst_id,
                    state=status,
                    model_source=inst_source,
                    healthy=healthy,
                    message=None,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                ).model_dump()
            )

        # Drift circuit breaker bookkeeping (C1): count consecutive
        # drift-driven REPLACE rounds; reset when the spec settles (or was
        # edited — the update route resets the counter in status_json).
        prev_attempts = getattr(intent.status, "drift_replace_attempts", 0) or 0
        if drift_replace:
            drift_attempts = prev_attempts + 1
        elif spec_settled:
            drift_attempts = 0
        else:
            drift_attempts = prev_attempts

        # Only the diff path can observe the tripped breaker, so the state is
        # carried on the status: a tick that returns through the settle window
        # or a rollout strategy never populates ``observed`` and would let the
        # phase flip back to Ready, flapping every other tick. It is cleared
        # when the spec settles (or is edited, via the update route).
        unsettled_keys: list[str] | set[str] = observed.get("_drift_unsettled") or []
        if not unsettled_keys and not spec_settled:
            unsettled_keys = list(
                getattr(intent.status, "drift_unsettled_keys", None) or []
            )
        if spec_settled:
            unsettled_keys = []

        # Determine phase
        current_phase = intent.status.phase.value
        if current_phase == "deleting":
            if observed_count == 0:
                phase = IntentPhase.DELETED
            else:
                phase = IntentPhase.DELETING
        elif (
            desired == 0
            and observed_count == 0
            or ready_count == desired
            and desired > 0
        ):
            phase = IntentPhase.READY
        elif ready_count == 0 and desired > 0 and observed_count == 0:
            phase = IntentPhase.RECONCILING
        elif ready_count > 0:
            phase = IntentPhase.DEGRADED
        elif ready_count == 0 and (observed_count > 0 or desired > 0):
            phase = IntentPhase.FAILED
        else:
            phase = IntentPhase.RECONCILING

        # A tripped drift circuit breaker means the replicas provably do not
        # match the spec — never report the deployment as converged.
        if unsettled_keys and phase not in (
            IntentPhase.DELETING,
            IntentPhase.DELETED,
        ):
            phase = IntentPhase.DEGRADED

        # C3: specific reason for partial fulfillment, rendered in the
        # Degraded condition message when a cause is identifiable.
        shortfall_reason = _shortfall_reason(intent, observed)

        # Build conditions
        conditions: list[dict[str, Any]] = []
        now_iso = datetime.now(timezone.utc).isoformat()
        if ready_count >= 1:
            conditions.append(
                Condition(
                    type="Available",
                    status=True,
                    reason="MinimumReplicasAvailable",
                    message=f"{ready_count}/{desired} ready",
                    last_transition=now_iso,
                ).model_dump()
            )
        if phase == IntentPhase.RECONCILING:
            conditions.append(
                Condition(
                    type="Progressing",
                    status=True,
                    reason="Reconciling",
                    message="Reconciliation in progress",
                    last_transition=now_iso,
                ).model_dump()
            )
        if phase == IntentPhase.DEGRADED:
            if unsettled_keys:
                conditions.append(
                    Condition(
                        type="Degraded",
                        status=True,
                        reason="DriftUnsettled",
                        message=(
                            f"backend config drift unsettled after "
                            f"{drift_attempts} REPLACE attempts — mismatching "
                            f"keys: {', '.join(sorted(unsettled_keys))}. "
                            f"Edit the spec to reset the counter."
                        ),
                        last_transition=now_iso,
                    ).model_dump()
                )
            else:
                conditions.append(
                    Condition(
                        type="Degraded",
                        status=True,
                        reason="ShortfallOrFailure",
                        message=(
                            f"{ready_count}/{desired} ready — {shortfall_reason}"
                            if shortfall_reason
                            else (
                                f"{ready_count}/{desired} ready — "
                                "desired replicas cannot all be made ready"
                            )
                        ),
                        last_transition=now_iso,
                    ).model_dump()
                )

        # Conflict condition for manual instances (§5.3)
        manual_conflicts = observed.get("manual_conflicts", [])
        if manual_conflicts:
            conflict_hosts = sorted(
                {
                    m.get("_host_name") or m.get("_host_id", "?")
                    for m in manual_conflicts
                }
            )
            conditions.append(
                Condition(
                    type="Conflict",
                    status=True,
                    reason="ManualInstanceConflict",
                    message=(
                        f"Manual instance(s) serving '{alias}' on host(s): "
                        f"{', '.join(conflict_hosts[:5])}"
                        f"{'...' if len(conflict_hosts) > 5 else ''}"
                    ),
                    last_transition=now_iso,
                ).model_dump()
            )

        # Build last_error
        last_error_model = None
        if last_error:
            last_error_model = LastError(
                code=last_error.get("code", "unknown"),
                message=last_error.get("message", ""),
                host_id=last_error.get("host_id"),
                source_uri=last_error.get("source_uri"),
                at=last_error.get("at", now_iso),
                instance_id=last_error.get("instance_id"),
                log_tail=last_error.get("log_tail"),
                recoverable=bool(last_error.get("recoverable", False)),
            )
        elif unsettled_keys:
            # The drift circuit breaker tripped: no single host action
            # failed, but the spec cannot be rolled out. Record the error
            # naming the mismatching keys so the loop ends in one clear,
            # actionable state instead of churning forever (C1).
            first_host = next((inst.get("_host_id") for inst in managed), None)
            last_error_model = LastError(
                code="BackendDriftUnsettled",
                message=(
                    f"backend config drift unsettled after {drift_attempts} "
                    f"REPLACE attempts; mismatching keys: "
                    f"{', '.join(sorted(unsettled_keys))}"
                ),
                host_id=first_host,
                source_uri=intent.model_source,
                at=now_iso,
            )

        # Build status_json
        # Shortfall = desired - placeable (accounting for displacement candidates)
        candidates = observed.get("candidates", [])
        displaceable_map = observed.get("displaceable_map", {})
        placeable = len(candidates) + len(displaceable_map)
        structural_shortfall = max(0, desired - observed_count - placeable)

        status_json = {
            "observed_replicas": observed_count,
            "ready_replicas": ready_count,
            "updated_replicas": updated_count,
            "available": ready_count >= 1,
            "shortfall": structural_shortfall,
            "replica_set": replica_set,
            "conditions": conditions,
            "strategy_progress": strategy_progress,
            "last_error": last_error_model.model_dump() if last_error_model else None,
            "spec_changed_at": None if spec_settled else _spec_version(intent),
            "drift_replace_attempts": drift_attempts,
            "drift_unsettled_keys": sorted(unsettled_keys),
            "shortfall_reason": shortfall_reason,
        }

        # Determine reconcile state
        if last_error:
            reconcile = ReconcileState.FAILED
        elif phase in (IntentPhase.READY, IntentPhase.DEGRADED):
            reconcile = ReconcileState.SUCCEEDED
        else:
            reconcile = ReconcileState.IN_PROGRESS

        now = datetime.now(timezone.utc)

        # Set ready_at when first reaching ready
        ready_at = None
        if phase == IntentPhase.READY:
            ready_at = (
                intent.status.ready_at if intent.status.ready_at else now.isoformat()
            )

        await intent_db.update_status(
            intent.id,
            phase=phase.value,
            reconcile=reconcile.value,
            status_json=status_json,
            last_reconciled_at=now,
            ready_at=ready_at,
            spec_version_seen=_spec_version(intent),
        )

        # ── Emit Socket.IO events for live WebUI updates (§10.4) ──
        try:
            from app.socketio_app import sio

            # Fetch the updated intent so the event carries the full record
            updated = await intent_db.get_intent(intent.id)
            if updated:
                await sio.emit(
                    "intent_update",
                    updated.model_dump(),
                    namespace="/webui",
                )
                if phase == IntentPhase.DELETED:
                    await sio.emit(
                        "intent_removed",
                        {"id": intent.id, "alias": intent.alias},
                        namespace="/webui",
                    )
        except Exception:
            logger.warning(
                "Failed to emit intent_update event for %s", intent.id, exc_info=True
            )


# ── Helpers ────────────────────────────────────────────────────


def _intent_phase(intent: Any) -> str:
    """Safely extract the intent phase as a string."""
    try:
        return intent.status.phase.value
    except (AttributeError, TypeError):
        return str(getattr(intent, "phase", "pending"))


def _intent_orphan(intent: Any) -> bool:
    """Check if the intent was deleted with orphan=true."""
    try:
        return intent.metadata.get("orphan") == "true"
    except (AttributeError, TypeError):
        return False


def _shortfall_reason(intent: Any, observed: dict[str, Any]) -> str | None:
    """Explain why the intent cannot be fully placed (C3).

    Produces a specific message for the Degraded condition instead of the
    generic 'desired replicas cannot all be made ready': a gpu_type with no
    matching host, an allow-list smaller than the replica count, a fully
    draining eligible fleet, or a VRAM request above the largest available
    capacity. Returns None when no specific cause is identifiable and the
    generic text remains the fallback.
    """
    desired = intent.replicas
    observed_count = len(observed.get("managed_instances", []))
    if desired <= observed_count:
        return None

    placement = intent.placement
    roles = list(placement.roles) if placement.roles else ["inference"]
    host_allow = list(getattr(placement, "host_allow", None) or [])
    host_deny = list(getattr(placement, "host_deny", None) or [])
    gpu_type = getattr(placement, "gpu_type", None)

    # An explicit allow-list smaller than the request can never be satisfied.
    if host_allow and len(host_allow) < desired:
        return (
            f"host_allow names {len(host_allow)} host(s), {desired} replicas requested"
        )

    hosts = observed.get("hosts", [])
    snapshots = observed.get("snapshots", {})

    from app.services.placement import filter_durable_hosts

    durable = filter_durable_hosts(
        hosts,
        roles=roles,
        gpu_type=gpu_type,
        host_allow=host_allow or None,
        host_deny=host_deny or None,
    )
    if not durable:
        if gpu_type:
            return f"no host matches gpu_type={gpu_type}"
        return f"no host matches roles {roles}"

    if all(h.drain_state is not None for h in durable):
        return f"all {len(durable)} eligible host(s) are draining"

    # Capacity: the largest effective VRAM (unified memory folds into RAM)
    # among durably eligible hosts, against the requested amount.
    vram_gb = float(getattr(intent.resources, "vram_gb", None) or 0)
    if vram_gb > 0:
        largest_vram = 0.0
        for h in durable:
            snap = snapshots.get(h.id)
            if snap is None:
                continue
            eff = (
                snap.vram_available_gb
                if snap.vram_available_gb is not None
                else (snap.ram_available_gb or 0.0)
            )
            largest_vram = max(largest_vram, eff or 0.0)
        if vram_gb > largest_vram:
            return (
                f"needs {vram_gb:g} GB VRAM, largest available among "
                f"eligible hosts is {largest_vram:g} GB"
            )

    return None


def _spec_version(intent: Any) -> str | None:
    """Return a marker that changes when the intent's spec is edited.

    ``status.updated_at`` cannot serve here — it moves on every status
    write. ``spec_changed_at`` is stamped only by an actual spec update
    (S-044), which is what makes it usable as a spec identity.
    """
    return getattr(getattr(intent, "status", None), "spec_changed_at", None)


def _is_drift_replace(action: Action) -> bool:
    """Whether *action* is the drift-driven REPLACE the C1 breaker counts."""
    return action.type == ActionType.REPLACE and action.reason == "backend config drift"


def _spec_settled(observed: dict[str, Any], actions: list[Action]) -> bool:
    """Whether a pending spec change can be considered rolled out (S-044).

    Two things have to hold: no replica still needs replacing, and every
    replica could actually be compared against the spec. A replica whose real
    configuration could not be read is not evidence of agreement — treating it
    as settled would clear ``spec_changed_at`` and lose the edit, leaving the
    replica on the old config while the intent reports itself up to date.

    A tripped drift circuit breaker (``_drift_unsettled``) also keeps the
    edit pending: the replicas provably do not match the spec, and clearing
    the marker would drop the edit while the drift persists.
    """
    if any(a.type == ActionType.REPLACE for a in actions):
        return False
    if observed.get("_drift_unsettled"):
        return False
    return not any(
        inst.get("_full_config_unknown") for inst in observed["managed_instances"]
    )


def _detect_backend_drift(intent: Any, instance_config: dict[str, Any]) -> list[str]:
    """Check if the instance's backend config has drifted from the intent.

    Returns the list of drifted backend keys (empty means no drift).

    Compares the intent's backend fields (excluding identity/server-derived
    fields) against the instance config for the same keys.

    NB: the WS instances cache is intentionally flat (id/alias/status/port/
    backend_type/model_source/... only — see gateway._ws_cache_from_http_
    instances and the host's send_instances_update), so most backend fields
    (device, dtype, max_length, labels) are never visible here. Fields that
    are absent from the cache entry are skipped; comparing them as None
    would report drift on every managed instance and trigger a REPLACE-stop
    loop (the pre-fix behavior).
    """
    intent_backend = intent.backend if isinstance(intent.backend, dict) else {}
    _skip_keys = {
        "backend_type",
        "alias",
        "model_source",
        "host",
        "port",
        "api_key",
        "managed_by",
        "intent_id",
        "model",
        "model_id",
    }
    drifted: list[str] = []
    for key, value in intent_backend.items():
        if key in _skip_keys:
            continue
        if key not in instance_config:
            continue  # flat WS cache does not carry this field
        inst_value = instance_config.get(key)
        if not _backend_value_matches(value, inst_value):
            drifted.append(key)
    return drifted


def _jsonish(value: Any) -> Any:
    """Parse a JSON-looking string; pass JSON-native values through; else None.

    Scalars (bool/int/float) pass through so a string-typed spec value can be
    compared against the host's coerced value: C1's premise is host-side
    Pydantic coercion, which turns ``"true"`` into ``True`` and ``"99"`` into
    ``99``. Excluding them from this layer left exactly those cases reading as
    drift.

    The sentinel is None, so a JSON literal null (which parses to None) is
    indistinguishable from 'not JSON' — acceptable, since a null spec value
    equals a None instance value via the exact equality fast path anyway.
    """
    if isinstance(value, (dict, list, bool, int, float)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return None


def _coerce_jsonish(value: Any) -> Any:
    """Recursively turn boolean-looking strings into real booleans.

    Mirrors ``solar_host.models.llamacpp._coerce_template_kwargs``: the host
    rewrites ``chat_template_kwargs`` at the config boundary, so a spec that
    carries a dict, a spaced JSON string, or string-typed booleans must
    compare equal to the host's compact canonical form. Control cannot
    import ``solar_host``; the duplication is intentional and pinned by a
    test that asserts both behave identically.
    """
    if isinstance(value, dict):
        return {k: _coerce_jsonish(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_coerce_jsonish(v) for v in value]
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return value


def _strip_relative_prefix(value: str) -> str:
    """Drop leading './' segments from a relative spec path.

    ``lstrip("./")`` takes a *set* of characters, so it would eat leading dots
    and slashes indiscriminately — turning '.gitignore' into 'gitignore' and
    then failing to match a path that really ends in '/.gitignore'.
    """
    while value.startswith("./"):
        value = value[2:]
    return value


def _backend_value_matches(spec_value: Any, inst_value: Any) -> bool:
    """Compare a backend spec value against the instance's reported value.

    Three comparison layers, tried in order:

    1. Exact equality (unchanged fast path).
    2. JSON-structural equality: when either side is a dict/list or a string
       that parses as JSON, both sides are parsed (where needed) and
       compared after applying the same recursive boolean coercion the host
       applies at the config boundary (``_coerce_jsonish``). This tolerates
       the host re-serializing ``chat_template_kwargs`` as compact canonical
       JSON with real booleans.
    3. Path/glob equality: the instance's full config carries resolve-time
       artifacts — absolute paths the host resolved from the spec's
       patterns. A bare filename (e.g. mmproj: 'mmproj-BF16.gguf'), a glob
       ('*mmproj-BF16*.gguf') or a relative path ('sub/mmproj.gguf') in the
       spec that matches the tail of the instance's resolved path is a
       match. Treating these as drift flags every replacement while a spec
       change is pending, trapping the intent in a REPLACE-stop churn that
       never lets the edit settle.
    """
    if spec_value == inst_value:
        return True

    parsed_spec = _jsonish(spec_value)
    parsed_inst = _jsonish(inst_value)
    if parsed_spec is not None and parsed_inst is not None:
        return _coerce_jsonish(parsed_spec) == _coerce_jsonish(parsed_inst)

    if isinstance(spec_value, str) and isinstance(inst_value, str):
        if any(c in spec_value for c in "*?["):
            # A pattern with a directory component must be matched against a
            # matching number of trailing path segments, not the basename:
            # 'models/*.gguf' can never equal a basename.
            depth = spec_value.replace(os.sep, "/").count("/") + 1
            tail = "/".join(inst_value.replace(os.sep, "/").split("/")[-depth:])
            return fnmatch.fnmatch(tail, spec_value.replace(os.sep, "/"))
        return inst_value.endswith("/" + _strip_relative_prefix(spec_value))
    return False


# Singleton
reconciler = Reconciler()
