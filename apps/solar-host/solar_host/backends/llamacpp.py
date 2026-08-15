"""LlamaCpp backend runner implementation."""

import re
from datetime import UTC, datetime
from typing import Any

from solar_host.backends.base import BackendRunner, RuntimeStateUpdate
from solar_host.backends.prom import parse_prometheus
from solar_host.config import settings
from solar_host.models.base import (
    GenerationMetrics,
    InstancePhase,
    InstanceUsageSnapshot,
)

# Tolerant matcher covering both the `llama_server:` and the newer
# `main: server is listening` forms, any host/port. This is the readiness
# contract: the lifecycle status only moves starting -> running on it.
_RE_READY = re.compile(
    r"(?:llama[_ ]server|main)\s*:\s*(?:server is )?listening on https?://",
    re.IGNORECASE,
)


class LlamaCppRunner(BackendRunner):
    """Backend runner for llama.cpp server instances."""

    def __init__(self):
        # Compile regex patterns for parsing llama-server logs
        self._re_launch = re.compile(
            r"slot\s+launch_slot_:\s+id\s+(\d+)\s*\|\s*task\s+(-?\d+)\s*\|\s*processing task"
        )
        self._re_progress = re.compile(
            r"prompt processing progress.*progress\s*=\s*([0-9.]+)"
        )
        self._re_prompt_done = re.compile(r"\|\s*prompt done\b")
        # ``stop processing n_tokens`` carries the slot's exact total token
        # count at release, from which the per-request token split is
        # derived. Older builds print the release line without it; the
        # dedicated legacy pattern still matches those.
        self._re_release = re.compile(
            r"slot\s+release:\s+id\s+(\d+)\s*\|\s*task\s+(-?\d+)\s*\|\s*stop processing.*n_tokens\s*=\s*(\d+)"
        )
        self._re_release_legacy = re.compile(
            r"slot\s+release:\s+id\s+(\d+)\s*\|\s*task\s+(-?\d+)\s*\|\s*stop processing"
        )
        self._re_all_idle = re.compile(r"srv\s+update_slots:\s+all slots are idle")
        self._re_new_prompt = re.compile(
            r"slot\s+update_slots:\s+id\s+(\d+)\s*\|\s*task\s+(-?\d+)\s*\|\s*new prompt.*task\.n_tokens\s*=\s*(\d+)"
        )
        self._re_checkpoint = re.compile(
            r"created context checkpoint\s+(\d+)\s+of\s+(\d+)"
        )
        # Newer builds prefix every timing line with the slot/task header;
        # the remainder is dispatched on by _handle_timing_rest. This must
        # match BEFORE the legacy timing line pattern below.
        self._re_print_timing = re.compile(
            r"slot\s+print_timing:\s+id\s+(\d+)\s*\|\s*task\s+(-?\d+)\s*\|"
        )
        self._re_prompt_processing = re.compile(
            r"prompt processing,\s*n_tokens\s*=\s*(\d+),\s*progress\s*=\s*([0-9.]+)"
        )
        self._re_ndecoded = re.compile(
            r"n_decoded\s*=\s*(\d+),\s*tg\s*=\s*([0-9.]+)\s*t/s"
        )
        self._re_prompt_eval_time = re.compile(
            r"prompt eval time\s*=\s*([0-9.]+)\s*ms\s*/\s*(\d+)\s*tokens"
        )
        # ``(?<!prompt )`` keeps this from also matching the prompt eval
        # line, whose remainder contains ``eval time`` as a substring — the
        # old unanchored pattern mis-attributed prompt eval as decode.
        self._re_eval_time = re.compile(
            r"(?<!prompt )eval time\s*=\s*([0-9.]+)\s*ms\s*/\s*(\d+)\s*tokens"
            r"\s*\(\s*([0-9.]+)\s*ms per token,\s*([0-9.]+)\s*tokens per second\)"
        )

    def get_backend_type(self) -> str:
        return "llamacpp"

    def is_ready_line(self, line: str) -> bool:
        """True when the line proves llama-server is listening."""
        return _RE_READY.search(line) is not None

    def build_command(self, instance: Any) -> list[str]:
        """Build llama-server command from instance config."""
        config = instance.config
        cmd = [
            "llama-server",
            "--model",
            config.model,
            "--alias",
            config.alias,
            "--threads",
            str(config.threads),
            "--n_gpu_layers",
            str(config.n_gpu_layers),
            "--temp",
            str(config.temp),
            "--top_p",
            str(config.top_p),
            "--top_k",
            str(config.top_k),
            "--min_p",
            str(config.min_p),
            "--ctx-size",
            str(config.ctx_size),
            "--host",
            config.host,
            "--port",
            str(instance.port),
            "--api-key",
            settings.api_key,
            "--no-warmup",
        ]

        cmd.extend(self._multi_gpu_args(config))

        if config.chat_template_file:
            cmd.extend(["--jinja", "--chat-template-file", config.chat_template_file])
        else:
            cmd.extend(["--jinja"])

        chat_template_kwargs = getattr(config, "chat_template_kwargs", None)
        if chat_template_kwargs and chat_template_kwargs.strip():
            cmd.extend(["--chat-template-kwargs", chat_template_kwargs.strip()])

        reasoning = getattr(config, "reasoning", None)
        if reasoning and reasoning.strip():
            cmd.extend(["--reasoning", reasoning.strip()])

        reasoning_budget = getattr(config, "reasoning_budget", None)
        if reasoning_budget is not None:
            cmd.extend(["--reasoning-budget", str(int(reasoning_budget))])

        cmd.extend(self._speculative_args(config))

        cache_type_k = getattr(config, "cache_type_k", None)
        if cache_type_k and cache_type_k.strip():
            cmd.extend(["-ctk", cache_type_k.strip()])

        cache_type_v = getattr(config, "cache_type_v", None)
        if cache_type_v and cache_type_v.strip():
            cmd.extend(["-ctv", cache_type_v.strip()])

        rope_scaling = getattr(config, "rope_scaling", None)
        if rope_scaling and rope_scaling.strip():
            cmd.extend(["--rope-scaling", rope_scaling.strip()])

        rope_scale = getattr(config, "rope_scale", None)
        if rope_scale is not None:
            cmd.extend(["--rope-scale", str(rope_scale)])

        yarn_orig_ctx = getattr(config, "yarn_orig_ctx", None)
        if yarn_orig_ctx is not None:
            cmd.extend(["--yarn-orig-ctx", str(int(yarn_orig_ctx))])

        if getattr(config, "special", False):
            cmd.append("--special")

        ot_value = getattr(config, "ot", None)
        if ot_value and ot_value.strip():
            cmd.extend(["-ot", ot_value])

        mmproj = getattr(config, "mmproj", None)
        if mmproj and mmproj.strip():
            cmd.extend(["--mmproj", mmproj.strip()])
            if not getattr(config, "mmproj_offload", True):
                cmd.append("--no-mmproj-offload")

        # Model type flags
        model_type = getattr(config, "model_type", "llm")
        if model_type == "embedding":
            cmd.append("--embedding")
        elif model_type == "reranker":
            cmd.append("--rerank")

        # Pooling flag (only valid for embedding models)
        if model_type == "embedding":
            pooling = getattr(config, "pooling", None)
            if pooling and pooling.strip():
                cmd.extend(["--pooling", pooling])

        # Prometheus metrics endpoint with per-slot gauges: the /metrics
        # counters are the authoritative busy signal and the exact source of
        # per-request token counts for the metrics poll loop.
        cmd.extend(["--metrics", "--slots"])

        return cmd

    @staticmethod
    def _multi_gpu_args(config: Any) -> list[str]:
        """Build the device/split flags for a multi-GPU host."""
        args: list[str] = []

        devices = getattr(config, "devices", None)
        if devices and devices.strip():
            args.extend(["--device", devices.strip()])

        split_mode = getattr(config, "split_mode", None)
        if split_mode:
            args.extend(["--split-mode", split_mode])

        tensor_split = getattr(config, "tensor_split", None)
        if tensor_split and tensor_split.strip():
            args.extend(["--tensor-split", tensor_split.strip()])

        main_gpu = getattr(config, "main_gpu", None)
        if main_gpu is not None:
            args.extend(["--main-gpu", str(int(main_gpu))])

        return args

    @staticmethod
    def _speculative_args(config: Any) -> list[str]:
        """Build the --spec-* flags for the configured speculative decoding.

        Only generation models speculate: llama-server rejects the flags for
        an --embedding or --rerank server. An incomplete config yields no
        flags at all, since a --spec-type without the file it needs makes
        llama-server exit instead of falling back to plain decoding.
        """
        if getattr(config, "model_type", "llm") != "llm":
            return []

        spec_type = getattr(config, "spec_type", None)
        spec_draft_model = getattr(config, "spec_draft_model", None)
        spec_draft_n_max = getattr(config, "spec_draft_n_max", None)
        spec_draft_conf_min = getattr(config, "spec_draft_conf_min", None)

        if spec_type == "draft-mtp":
            if spec_draft_n_max is None:
                return []
            return [
                "--spec-type",
                spec_type,
                "--spec-draft-n-max",
                str(int(spec_draft_n_max)),
            ]

        if spec_type == "draft-dspark":
            if not (spec_draft_model and spec_draft_model.strip()):
                return []
            args = [
                "--spec-type",
                spec_type,
                "--spec-draft-model",
                spec_draft_model.strip(),
            ]
            if spec_draft_n_max is not None:
                args.extend(["--spec-draft-n-max", str(int(spec_draft_n_max))])
            if spec_draft_conf_min is not None:
                args.extend(["--spec-draft-conf-min", str(float(spec_draft_conf_min))])
            return args

        return []

    def get_health_endpoint(self) -> str:
        return "/health"

    def get_supported_endpoints(self) -> list[str]:
        return [
            "/v1/chat/completions",
            "/v1/completions",
            "/v1/models",
            "/v1/embeddings",
            "/v1/rerank",
        ]

    def get_supported_endpoints_for_model_type(self, model_type: str) -> list[str]:
        """Return endpoints based on llama.cpp model_type (llm, embedding, reranker)."""
        if model_type == "embedding":
            return ["/v1/embeddings", "/v1/models"]
        elif model_type == "reranker":
            return ["/v1/rerank", "/v1/models"]
        return ["/v1/chat/completions", "/v1/completions", "/v1/models"]

    def initialize_context(self) -> dict[str, Any]:
        """Initialize parsing context for llama.cpp log parsing."""
        return {
            "active_slots": set(),
            "pending_generations_by_slot": {},
            "recent_generations": [],
            "last_state": {
                "busy": False,
                "phase": InstancePhase.IDLE.value,
                "prefill_progress": None,
                "active_slots": 0,
                "slot_id": None,
                "task_id": None,
            },
        }

    def parse_log_line(
        self, instance_id: str, line: str, context: dict[str, Any]
    ) -> RuntimeStateUpdate | None:
        """Parse a llama-server log line and return state update if changed."""
        slots: set[int] = context.get("active_slots", set())
        last_state: dict[str, Any] = context.get("last_state", {})
        pending_by_slot: dict[int, dict[str, Any]] = context.get(
            "pending_generations_by_slot", {}
        )

        # slot launch → add slot, busy true; create the pending entry so a
        # generation is tracked even when its timing lines arrive without a
        # preceding `new prompt` line (the current llama.cpp build's shape)
        m = self._re_launch.search(line)
        if m:
            try:
                slot_id = int(m.group(1))
                task_id = int(m.group(2))
            except Exception:  # noqa: BLE001
                slot_id, task_id = -1, None
            slots.add(slot_id)
            context["active_slots"] = slots
            if task_id is not None:
                self._ensure_pending(context, slot_id, task_id)
            return self._create_update(
                busy=True,
                phase=InstancePhase.PREFILL,
                prefill_progress=last_state.get("prefill_progress"),
                active_slots=len(slots),
                slot_id=slot_id,
                task_id=task_id if task_id is not None else last_state.get("task_id"),
                last_state=last_state,
                context=context,
            )

        # new prompt → phase becomes prefill; capture task_id and prompt tokens
        m = self._re_new_prompt.search(line)
        if m:
            try:
                slot_id = int(m.group(1))
                task_id = int(m.group(2))
                prompt_tokens = int(m.group(3))
            except Exception:  # noqa: BLE001
                slot_id, task_id, prompt_tokens = -1, -1, None
            slots.add(slot_id)
            context["active_slots"] = slots

            # Initialize pending generation metrics for this slot
            self._ensure_pending(context, slot_id, task_id)
            pending = pending_by_slot.get(slot_id) or {}
            if prompt_tokens is not None:
                pending["prompt_tokens"] = prompt_tokens
                pending_by_slot[slot_id] = pending
                context["pending_generations_by_slot"] = pending_by_slot

            return self._create_update(
                busy=True,
                phase=InstancePhase.PREFILL,
                prefill_progress=0.0,
                active_slots=len(slots),
                slot_id=slot_id,
                task_id=task_id,
                prefill_prompt_tokens=prompt_tokens,
                last_state=last_state,
                context=context,
            )

        # prompt processing progress → update progress
        m = self._re_progress.search(line)
        if m:
            try:
                progress = float(m.group(1))
            except Exception:  # noqa: BLE001
                progress = None
            return self._create_update(
                busy=True if len(slots) > 0 else last_state.get("busy", False),
                phase=InstancePhase.PREFILL,
                prefill_progress=progress,
                active_slots=len(slots),
                slot_id=last_state.get("slot_id"),
                task_id=last_state.get("task_id"),
                last_state=last_state,
                context=context,
            )

        # prompt done → set progress to 1.0
        if self._re_prompt_done.search(line):
            return self._create_update(
                busy=True if len(slots) > 0 else last_state.get("busy", False),
                phase=(
                    InstancePhase.GENERATING if len(slots) > 0 else InstancePhase.IDLE
                ),
                prefill_progress=1.0,
                active_slots=len(slots),
                slot_id=last_state.get("slot_id"),
                task_id=last_state.get("task_id"),
                last_state=last_state,
                context=context,
            )

        # context checkpoint progress (still prefill phase)
        m = self._re_checkpoint.search(line)
        if m:
            try:
                idx = int(m.group(1))
                total = int(m.group(2))
            except Exception:  # noqa: BLE001
                idx, total = None, None
            return self._create_update(
                busy=True if len(slots) > 0 else last_state.get("busy", False),
                phase=InstancePhase.PREFILL,
                prefill_progress=last_state.get("prefill_progress"),
                active_slots=len(slots),
                slot_id=last_state.get("slot_id"),
                task_id=last_state.get("task_id"),
                checkpoint_index=idx,
                checkpoint_total=total,
                last_state=last_state,
                context=context,
            )

        # Newer llama.cpp builds prefix every timing line with
        # `slot print_timing: id N | task M |`; strip the header and dispatch
        # on the remainder (progress, live decode, prompt eval, final eval).
        prefix = self._re_print_timing.search(line)
        if prefix:
            try:
                slot_id = int(prefix.group(1))
                task_id = int(prefix.group(2))
            except Exception:  # noqa: BLE001
                slot_id, task_id = -1, None
            # A timing line proves the slot is active (the fixture can start
            # mid-generation with no launch line), so track it for
            # active_slots/phase purposes.
            slots.add(slot_id)
            context["active_slots"] = slots
            # A generation can enter the log mid-stream (no launch line
            # visible), so every prefixed line ensures the pending entry.
            if task_id is not None:
                self._ensure_pending(context, slot_id, task_id)
            return self._handle_timing_rest(
                instance_id,
                line,
                context,
                slots,
                last_state,
                pending_by_slot,
                slot_id,
                task_id,
                line[prefix.end() :],
            )

        # Legacy (pre-header) decode timing line, kept for older builds that
        # print `llama_print_timings: eval time = ...` bare.
        m = self._re_eval_time.search(line)
        if m:
            try:
                gen_tokens = int(m.group(2))
                ms_per_tok = float(m.group(3))
                tps = float(m.group(4))
            except Exception:  # noqa: BLE001
                gen_tokens, ms_per_tok, tps = None, None, None

            # Update pending metrics for last active slot
            last_slot_id = last_state.get("slot_id")
            if isinstance(last_slot_id, int):
                pending: dict[str, Any] = pending_by_slot.get(last_slot_id) or {
                    "slot_id": last_slot_id
                }
                if gen_tokens is not None:
                    pending["generated_tokens"] = gen_tokens
                if tps is not None:
                    pending["decode_tps"] = tps
                if ms_per_tok is not None:
                    pending["decode_ms_per_token"] = ms_per_tok
                pending_by_slot[last_slot_id] = pending
                context["pending_generations_by_slot"] = pending_by_slot

            phase_str = last_state.get("phase", InstancePhase.IDLE.value)
            phase = (
                InstancePhase(phase_str)
                if phase_str in [p.value for p in InstancePhase]
                else InstancePhase.IDLE
            )
            if len(slots) > 0:
                phase = InstancePhase.GENERATING

            return self._create_update(
                busy=True if len(slots) > 0 else last_state.get("busy", False),
                phase=phase,
                prefill_progress=last_state.get("prefill_progress"),
                active_slots=len(slots),
                slot_id=last_state.get("slot_id"),
                task_id=last_state.get("task_id"),
                generated_tokens=gen_tokens,
                decode_tps=tps,
                decode_ms_per_token=ms_per_tok,
                last_state=last_state,
                context=context,
            )

        # slot release → remove slot; if none remain, clear busy and progress.
        # The release line carries the slot's exact total n_tokens, the
        # anchor for the per-request token split at finalize time.
        release_n_tokens: int | None = None
        slot_id = -1
        m = self._re_release.search(line)
        if m:
            try:
                slot_id = int(m.group(1))
                release_n_tokens = int(m.group(3))
            except Exception:  # noqa: BLE001, S110
                pass
        else:
            m = self._re_release_legacy.search(line)
            if m:
                try:
                    slot_id = int(m.group(1))
                except Exception:  # noqa: BLE001, S110
                    pass

        if m:
            if slot_id in slots:
                slots.discard(slot_id)
            context["active_slots"] = slots

            # Finalize any pending generation for this slot
            pending: dict[str, Any] | None = pending_by_slot.pop(slot_id, None)
            if pending is not None:
                metrics = self._finalize_llamacpp_generation(
                    instance_id, pending, release_n_tokens
                )
                recent = context.get("recent_generations", [])
                recent.append(metrics)
                # Keep only last 100 generations
                if len(recent) > 100:
                    recent = recent[-100:]
                context["recent_generations"] = recent
            context["pending_generations_by_slot"] = pending_by_slot

            if len(slots) == 0:
                return self._create_update(
                    busy=False,
                    phase=InstancePhase.IDLE,
                    prefill_progress=None,
                    active_slots=0,
                    slot_id=None,
                    task_id=None,
                    checkpoint_index=None,
                    checkpoint_total=None,
                    last_state=last_state,
                    context=context,
                )
            else:
                phase_str = last_state.get("phase", InstancePhase.GENERATING.value)
                phase = (
                    InstancePhase(phase_str)
                    if phase_str in [p.value for p in InstancePhase]
                    else InstancePhase.GENERATING
                )
                return self._create_update(
                    busy=True,
                    phase=phase,
                    prefill_progress=last_state.get("prefill_progress"),
                    active_slots=len(slots),
                    slot_id=last_state.get("slot_id"),
                    task_id=last_state.get("task_id"),
                    last_state=last_state,
                    context=context,
                )

        # all slots idle → force clear
        if self._re_all_idle.search(line):
            slots.clear()
            context["active_slots"] = slots
            return self._create_update(
                busy=False,
                phase=InstancePhase.IDLE,
                prefill_progress=None,
                active_slots=0,
                slot_id=None,
                task_id=None,
                checkpoint_index=None,
                checkpoint_total=None,
                last_state=last_state,
                context=context,
            )

        return None

    def _ensure_pending(
        self,
        context: dict[str, Any],
        slot_id: int,
        task_id: int,
    ) -> dict[str, Any]:
        """Create or refresh the pending generation entry for a slot.

        Only ``new prompt`` used to create entries, and that line no longer
        fires on the current llama.cpp build. A generation can also enter
        the log mid-stream without any launch line, so every prefixed timing
        line and the launch itself ensure the entry exists; a new task_id on
        a reused slot replaces the previous entry.
        """
        pending_by_slot = context.get("pending_generations_by_slot", {})
        pending = pending_by_slot.get(slot_id)
        if pending is None or pending.get("task_id") != task_id:
            pending = {
                "slot_id": slot_id,
                "task_id": task_id,
                "started_at": datetime.now(UTC).isoformat(),
            }
            pending_by_slot[slot_id] = pending
            context["pending_generations_by_slot"] = pending_by_slot
        return pending

    def _handle_timing_rest(
        self,
        instance_id: str,
        line: str,
        context: dict[str, Any],
        slots: set[int],
        last_state: dict[str, Any],
        pending_by_slot: dict[int, dict[str, Any]],
        slot_id: int,
        task_id: int | None,
        rest: str,
    ) -> RuntimeStateUpdate | None:
        """Dispatch on the remainder of a ``slot print_timing`` header line.

        The current build emits four timing lines per finished request, all
        sharing the header. ``prompt processing`` feeds the live prefill
        progress; ``n_decoded`` is the live decode signal (missing entirely
        before this change); ``prompt eval time`` records the uncached
        prompt portion; ``eval time`` records the final generated-token
        count and decode speed. ``total time`` / ``graphs reused`` change no
        state and return None.
        """
        pending: dict[str, Any]

        # prompt processing, n_tokens = X, progress = P
        m = self._re_prompt_processing.search(rest)
        if m:
            try:
                progress = float(m.group(2))
            except Exception:  # noqa: BLE001
                progress = None
            return self._create_update(
                busy=True,
                phase=InstancePhase.PREFILL,
                prefill_progress=progress,
                active_slots=len(slots),
                slot_id=slot_id,
                task_id=task_id,
                last_state=last_state,
                context=context,
            )

        # n_decoded = N, tg = T t/s → live generated-token count and TPS
        m = self._re_ndecoded.search(rest)
        if m:
            try:
                n_decoded = int(m.group(1))
                tps = float(m.group(2))
            except Exception:  # noqa: BLE001
                n_decoded, tps = None, None
            pending = pending_by_slot.get(slot_id) or {
                "slot_id": slot_id,
                "task_id": task_id,
            }
            if tps is not None:
                pending["decode_tps"] = tps
                pending["decode_ms_per_token"] = (1000.0 / tps) if tps > 0 else None
            pending_by_slot[slot_id] = pending
            context["pending_generations_by_slot"] = pending_by_slot
            return self._create_update(
                busy=True,
                phase=InstancePhase.GENERATING,
                prefill_progress=last_state.get("prefill_progress"),
                active_slots=len(slots),
                slot_id=slot_id,
                task_id=task_id,
                generated_tokens=n_decoded,
                decode_tps=tps,
                last_state=last_state,
                context=context,
            )

        # prompt eval time = ... ms / N tokens → uncached prompt portion
        m = self._re_prompt_eval_time.search(rest)
        if m:
            try:
                prompt_eval_tokens = int(m.group(2))
            except Exception:  # noqa: BLE001
                prompt_eval_tokens = None
            if prompt_eval_tokens is not None:
                pending = pending_by_slot.get(slot_id) or {
                    "slot_id": slot_id,
                    "task_id": task_id,
                }
                pending["prompt_eval_tokens"] = prompt_eval_tokens
                pending_by_slot[slot_id] = pending
                context["pending_generations_by_slot"] = pending_by_slot
            return None

        # eval time = ... ms / N tokens (...) → final generated-token count
        m = self._re_eval_time.search(rest)
        if m:
            try:
                gen_tokens = int(m.group(2))
                ms_per_tok = float(m.group(3))
                tps = float(m.group(4))
            except Exception:  # noqa: BLE001
                gen_tokens, ms_per_tok, tps = None, None, None
            pending = pending_by_slot.get(slot_id) or {
                "slot_id": slot_id,
                "task_id": task_id,
            }
            if gen_tokens is not None:
                pending["generated_tokens"] = gen_tokens
                pending["decode_tps"] = tps
                pending["decode_ms_per_token"] = ms_per_tok
                pending_by_slot[slot_id] = pending
                context["pending_generations_by_slot"] = pending_by_slot

            phase_str = last_state.get("phase", InstancePhase.IDLE.value)
            phase = (
                InstancePhase(phase_str)
                if phase_str in [p.value for p in InstancePhase]
                else InstancePhase.IDLE
            )
            if len(slots) > 0:
                phase = InstancePhase.GENERATING
            return self._create_update(
                busy=True if len(slots) > 0 else last_state.get("busy", False),
                phase=phase,
                prefill_progress=last_state.get("prefill_progress"),
                active_slots=len(slots),
                slot_id=slot_id,
                task_id=task_id,
                generated_tokens=gen_tokens,
                decode_tps=tps,
                decode_ms_per_token=ms_per_tok,
                last_state=last_state,
                context=context,
            )

        return None

    def _finalize_llamacpp_generation(
        self,
        instance_id: str,
        pending: dict[str, Any],
        release_n_tokens: int | None,
    ) -> GenerationMetrics:
        """Build the per-request GenerationMetrics at slot release.

        Token semantics (OpenAI, verified on the production sample):
          prompt_tokens      = release n_tokens - eval time tokens
          prompt_eval_tokens = prompt eval time tokens (uncached portion)
          cached_tokens      = prompt_tokens - prompt_eval_tokens (clamped
                               at 0: a long generation can over-count the
                               eval window by one token)
          generated_tokens   = eval time tokens
        """
        generated = pending.get("generated_tokens")
        prompt_tokens = None
        if generated is not None and release_n_tokens is not None:
            prompt_tokens = release_n_tokens - generated
        prompt_eval_tokens = pending.get("prompt_eval_tokens")
        cached_tokens = None
        if prompt_tokens is not None and prompt_eval_tokens is not None:
            cached_tokens = max(0, prompt_tokens - prompt_eval_tokens)
        total_tokens = release_n_tokens
        if total_tokens is None and prompt_tokens is not None and generated is not None:
            total_tokens = prompt_tokens + generated
        return GenerationMetrics(
            instance_id=instance_id,
            slot_id=pending.get("slot_id"),
            task_id=pending.get("task_id"),
            prompt_tokens=prompt_tokens,
            generated_tokens=generated,
            cached_tokens=cached_tokens,
            prompt_eval_tokens=prompt_eval_tokens,
            total_tokens=total_tokens,
            decode_tps=pending.get("decode_tps"),
            decode_ms_per_token=pending.get("decode_ms_per_token"),
            started_at=pending.get("started_at"),
            finished_at=datetime.now(UTC).isoformat(),
            source="log",
        )

    def _create_update(
        self,
        busy: bool,
        phase: InstancePhase,
        prefill_progress: float | None,
        active_slots: int,
        last_state: dict[str, Any],
        context: dict[str, Any],
        slot_id: int | None = None,
        task_id: int | None = None,
        prefill_prompt_tokens: int | None = None,
        generated_tokens: int | None = None,
        decode_tps: float | None = None,
        decode_ms_per_token: float | None = None,
        checkpoint_index: int | None = None,
        checkpoint_total: int | None = None,
    ) -> RuntimeStateUpdate | None:
        """Create a RuntimeStateUpdate if state has changed."""
        # Normalize prefill_progress
        pp: float | None = None
        if prefill_progress is not None:
            try:
                pp = float(prefill_progress)
            except Exception:  # noqa: BLE001
                pp = None

        # Check if state changed
        changed = (
            last_state.get("busy") != busy
            or last_state.get("phase") != phase.value
            or last_state.get("prefill_progress") != pp
            or last_state.get("active_slots") != active_slots
            or last_state.get("slot_id") != slot_id
            or last_state.get("task_id") != task_id
            or last_state.get("prefill_prompt_tokens") != prefill_prompt_tokens
            or last_state.get("generated_tokens") != generated_tokens
            or last_state.get("decode_tps") != decode_tps
            or last_state.get("decode_ms_per_token") != decode_ms_per_token
            or last_state.get("checkpoint_index") != checkpoint_index
            or last_state.get("checkpoint_total") != checkpoint_total
        )

        if not changed:
            return None

        # Update last_state in context
        context["last_state"] = {
            "busy": busy,
            "phase": phase.value,
            "prefill_progress": pp,
            "active_slots": active_slots,
            "slot_id": slot_id,
            "task_id": task_id,
            "prefill_prompt_tokens": prefill_prompt_tokens,
            "generated_tokens": generated_tokens,
            "decode_tps": decode_tps,
            "decode_ms_per_token": decode_ms_per_token,
            "checkpoint_index": checkpoint_index,
            "checkpoint_total": checkpoint_total,
        }

        return RuntimeStateUpdate(
            busy=busy,
            phase=phase,
            prefill_progress=pp,
            active_slots=active_slots,
            slot_id=slot_id,
            task_id=task_id,
            prefill_prompt_tokens=prefill_prompt_tokens,
            generated_tokens=generated_tokens,
            decode_tps=decode_tps,
            decode_ms_per_token=decode_ms_per_token,
            checkpoint_index=checkpoint_index,
            checkpoint_total=checkpoint_total,
        )

    def get_last_generation(self, context: dict[str, Any]) -> GenerationMetrics | None:
        """Get the last generation metrics from context."""
        recent = context.get("recent_generations", [])
        if not recent:
            return None
        return recent[-1]

    # llama.cpp exposes its counters through `/metrics` when launched with
    # --metrics. Counters use the `llamacpp:` colon namespace; the gauges
    # share the exposition without the prefix.
    _METRICS_MAP: tuple[tuple[str, str, type], ...] = (
        ("llamacpp:prompt_tokens_total", "prompt_tokens_total", int),
        ("llamacpp:tokens_predicted_total", "generated_tokens_total", int),
        ("llamacpp:requests_processing", "requests_processing", int),
        ("llamacpp:requests_deferred", "requests_deferred", int),
        ("llamacpp:kv_cache_usage_ratio", "kv_cache_usage_ratio", float),
    )

    def get_metrics_path(self) -> str | None:
        """Prometheus endpoint of llama-server (enabled via --metrics)."""
        return "/metrics"

    def parse_metrics(self, text: str) -> InstanceUsageSnapshot | None:
        """Parse the llama-server exposition into an usage snapshot."""
        values = parse_prometheus(text)
        kwargs: dict[str, Any] = {}
        for prom_name, attr, cast in self._METRICS_MAP:
            if prom_name in values:
                kwargs[attr] = cast(values[prom_name])
        if not kwargs:
            return None
        return InstanceUsageSnapshot(**kwargs)

    def apply_usage_snapshot(
        self,
        instance_id: str,
        context: dict[str, Any],
        snapshot: InstanceUsageSnapshot,
    ) -> RuntimeStateUpdate | None:
        """Drive the authoritative busy signal from requests_processing.

        ``requests_processing`` counts every request the server is working
        on (queued or in a slot), which log parsing can miss at the tail of
        a request. The counter only overrides the log-derived state when the
        two disagree; while they agree, the log's finer-grained phase
        (prefill vs generating) is kept.
        """
        if snapshot.requests_processing is None:
            return None
        busy = snapshot.requests_processing > 0
        last_state = context.get("last_state", {})
        if busy == last_state.get("busy", False):
            return None
        phase = InstancePhase.GENERATING if busy else InstancePhase.IDLE
        return self._create_update(
            busy=busy,
            phase=phase,
            prefill_progress=last_state.get("prefill_progress"),
            active_slots=snapshot.requests_processing,
            slot_id=last_state.get("slot_id"),
            task_id=last_state.get("task_id"),
            last_state=last_state,
            context=context,
        )
