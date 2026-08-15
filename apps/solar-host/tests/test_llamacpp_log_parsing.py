"""End-to-end llama.cpp log-parser tests over the production fixture.

The fixture (tests/fixtures/llamacpp_server.log) was captured from a live
llama-server: five finished generations, all timing lines carrying the
``slot print_timing: id N | task M |`` header. Replaying it must produce
exactly five GenerationMetrics with the exact token split, plus the
idle → prefill → generating → idle phase sequence per request.
"""

from itertools import pairwise
from pathlib import Path

import pytest

from solar_host.backends.llamacpp import LlamaCppRunner
from solar_host.models.base import GenerationMetrics, InstancePhase

FIXTURE = Path(__file__).parent / "fixtures" / "llamacpp_server.log"

# (task_id, prompt_tokens, prompt_eval_tokens, cached_tokens,
#  generated_tokens, total_tokens) — derived and verified against the
# fixture (release n_tokens minus eval tokens = OpenAI prompt semantics).
EXPECTED = [
    (25257, 36886, 4755, 32131, 1382, 38268),
    (26645, 24370, 24371, 0, 2057, 26427),
    (28718, 2727, 2728, 0, 336, 3063),
    (29059, 341, 59, 282, 72, 413),
    (29133, 341, 59, 282, 55, 396),
]


def _replay() -> tuple[LlamaCppRunner, dict]:
    runner = LlamaCppRunner()
    context = runner.initialize_context()
    for line in FIXTURE.read_text().splitlines():
        runner.parse_log_line("inst-1", line, context)
    return runner, context


def test_fixture_produces_five_finished_generations() -> None:
    _runner, context = _replay()

    assert len(context["recent_generations"]) == 5


def test_each_generation_has_the_exact_token_split() -> None:
    _runner, context = _replay()

    for metrics in context["recent_generations"]:
        assert isinstance(metrics, GenerationMetrics)
    by_task = {g.task_id: g for g in context["recent_generations"]}
    assert set(EXPECTED) == {
        (
            g.task_id,
            g.prompt_tokens,
            g.prompt_eval_tokens,
            g.cached_tokens,
            g.generated_tokens,
            g.total_tokens,
        )
        for g in context["recent_generations"]
    }
    # The plan's reference point: task 29059 splits 413 into 341 prompt /
    # 72 generated, of which 59 were evaluated and 282 came from the cache.
    g = by_task[29059]
    assert (g.prompt_tokens, g.prompt_eval_tokens, g.cached_tokens) == (341, 59, 282)
    assert g.generated_tokens == 72
    assert g.total_tokens == 413


def test_generation_task_has_request_and_slot_identity() -> None:
    _runner, context = _replay()

    g = context["recent_generations"][3]
    assert g.instance_id == "inst-1"
    assert g.task_id == 29059
    assert g.slot_id == 1
    assert g.source == "log"
    assert g.started_at is not None
    assert g.finished_at is not None


def test_the_cache_hit_portion_is_derived_not_misattributed() -> None:
    """Long generations re-evaluate the whole window: prompt_eval can exceed
    the OpenAI prompt count by one, and cached_tokens must clamp at 0 rather
    than going negative (the 26645 and 28718 requests)."""
    _runner, context = _replay()

    by_task = {g.task_id: g for g in context["recent_generations"]}
    assert by_task[26645].cached_tokens == 0
    assert by_task[28718].cached_tokens == 0
    assert by_task[25257].cached_tokens == 32131


def test_phase_transitions_idle_prefill_generating_idle() -> None:
    runner, context = _replay()
    transitions: list[str] = []
    for line in FIXTURE.read_text().splitlines():
        update = runner.parse_log_line("inst-1", line, context)
        if update:
            transitions.append(update.phase.value)

    # The fixture begins mid-generation (task 25257 is already decoding), so
    # the first observed phase is generating; every requested generation
    # then walks prefill -> generating -> idle, making each finished request
    # a clean idle border.
    assert transitions[0] == "generating"
    assert transitions[-1] == "idle"
    assert transitions.count("idle") == 5
    # Within each request the prefill phase precedes generating; a prefill
    # never appears between two generating phases of the same request.
    for prev, nxt in pairwise(transitions):
        assert nxt != "prefill" or prev in ("idle", "prefill")
    assert transitions.count("generating") >= 5


def test_live_decode_lines_update_tps_and_generated_count() -> None:
    """The n_decoded lines feed the live UI: generated_tokens counts up
    during generation and decode_tps is present, not only at release."""
    runner = LlamaCppRunner()
    context = runner.initialize_context()
    updates: list = []
    for line in FIXTURE.read_text().splitlines():
        update = runner.parse_log_line("inst-1", line, context)
        if update and update.decode_tps is not None and update.generated_tokens:
            updates.append(update)

    assert updates, "expected live decode updates during generation"
    assert updates[0].phase == InstancePhase.GENERATING
    # First samples belong to task 25257 (the fixture starts mid-generation:
    # two n_decoded lines then its final eval line), then task 26645's live
    # n_decoded samples count up.
    assert updates[0].generated_tokens == 915
    assert updates[0].decode_tps == pytest.approx(152.26)
    assert updates[2].generated_tokens == 1382  # task 25257 eval line
    assert updates[3].generated_tokens == 487
    assert updates[4].generated_tokens == 972  # grows over time
    assert updates[3].decode_tps == pytest.approx(162.10)


def test_release_without_timing_lines_still_clears_busy() -> None:
    """A slot release whose pending entry was never created (log started
    after the launch) must still drop the slot and return to idle."""
    runner = LlamaCppRunner()
    context = runner.initialize_context()
    runner.parse_log_line(
        "inst-1", "slot launch_slot_: id 0 | task 7 | processing task", context
    )
    update = runner.parse_log_line(
        "inst-1",
        "slot release: id 0 | task 7 | stop processing n_tokens = 100",
        context,
    )

    assert update is not None
    assert update.busy is False
    assert update.phase == InstancePhase.IDLE


def test_legacy_release_without_n_tokens_still_finalizes() -> None:
    """Older builds print the release line without n_tokens; the legacy
    pattern keeps release handling alive and the metrics keep whatever the
    timing lines provided."""
    runner = LlamaCppRunner()
    context = runner.initialize_context()
    runner.parse_log_line(
        "inst-1", "slot launch_slot_: id 1 | task 8 | processing task", context
    )
    runner.parse_log_line(
        "inst-1",
        "slot print_timing: id 1 | task 8 | prompt eval time = 10.00 ms / 5 tokens",
        context,
    )
    runner.parse_log_line(
        "inst-1",
        "slot print_timing: id 1 | task 8 | eval time = 11.00 ms / 3 tokens ( 3.67 ms per token, 272.73 tokens per second)",
        context,
    )
    runner.parse_log_line(
        "inst-1", "slot release: id 1 | task 8 | stop processing", context
    )

    g = runner.get_last_generation(context)
    assert g is not None
    assert g.generated_tokens == 3
    assert g.prompt_eval_tokens == 5
    assert g.prompt_tokens is None  # no release n_tokens to anchor the split
    assert g.total_tokens is None
