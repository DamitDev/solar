"""Tests for reconciliation engine (S-041)."""

import time
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.models.intent import (
    IntentPhase,
    IntentResponse,
    IntentStatus,
    PlacementConstraints,
    ReconcileState,
    ResourceRequirements,
)
from app.services.reconciliation import (
    Action,
    ActionType,
    Reconciler,
    StartOutcomeUnknown,
    _detect_backend_drift,
    _intent_orphan,
    _intent_phase,
    _spec_settled,
)

# ── Simple host stub ────────────────────────────────────────────


@dataclass
class _HostStub:
    id: str
    name: str = ""
    status: str = "online"
    url: str = "http://localhost:8080"
    api_key: str = "test-key"
    roles: list | None = None
    gpu_type: str | None = None
    drain_state: str | None = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = ["inference"]


@dataclass
class _SnapshotStub:
    """Minimal snapshot stub for placement policy."""

    host_id: str
    reachable: bool = True
    vram_available_gb: float = 10.0
    ram_available_gb: float | None = None
    disk_available_gb: float | None = None
    running_instance_count: int = 0


# ── Helpers ────────────────────────────────────────────────────


def _make_intent(**overrides) -> IntentResponse:
    """Build a minimal IntentResponse for testing."""
    defaults = {
        "id": "intent-001",
        "alias": "test-model",
        "model_source": "repo://test:v1",
        "replicas": 2,
        "priority": "production",
        "strategy": "rolling",
        "backend": {"backend_type": "huggingface_classification", "max_length": 512},
        "placement": PlacementConstraints(),
        "resources": ResourceRequirements(),
        "metadata": {},
        "status": IntentStatus(
            phase=IntentPhase.RECONCILING,
            reconcile=ReconcileState.IN_PROGRESS,
            desired_replicas=2,
        ),
    }
    defaults.update(overrides)
    return IntentResponse(**defaults)


def _make_managed_instance(
    instance_id: str,
    host_id: str = "host-1",
    host_name: str = "host1",
    alias: str = "test-model",
    model_source: str = "repo://test:v1",
    status: str = "running",
    **extra_config,
) -> dict:
    """Build a managed instance dict (as stored in Redis).

    Default config matches the default intent's backend so drift
    detection doesn't fire on tests that don't care about drift.
    """
    config = {
        "alias": alias,
        "model_source": model_source,
        "managed_by": "intent",
        "intent_id": "intent-001",
        "backend_type": "huggingface_classification",
        "max_length": 512,
    }
    config.update(extra_config)
    return {
        "instance_id": instance_id,
        "id": instance_id,
        "status": status,
        "config": config,
        "_host_id": host_id,
        "_host_name": host_name,
    }


def _make_observed(
    managed: list | None = None,
    alias_instances: list | None = None,
    hosts: list | None = None,
    snapshots: dict | None = None,
    gateway_aliases: set | None = None,
    candidates: list | None = None,
    displaceable_map: dict | None = None,
    manual_conflicts: list | None = None,
) -> dict:
    """Build an observed state dict for testing.

    For CREATE tests, provide *candidates* as a list of (host, snapshot) tuples.
    For tests that don't test CREATE, leave candidates empty.
    """
    if managed is None:
        managed = []
    if alias_instances is None:
        alias_instances = list(managed)
    if candidates is None:
        candidates = []
    return {
        "managed_instances": managed,
        "alias_instances": alias_instances,
        "hosts": hosts or [],
        "snapshots": snapshots or {},
        "gateway_aliases": gateway_aliases or set(),
        "candidates": candidates,
        "displaceable_map": displaceable_map or {},
        "manual_conflicts": manual_conflicts or [],
    }


# ── Helper function tests ──────────────────────────────────────


class TestHelpers:
    """Test standalone helper functions."""

    def test_intent_phase_extracts_correctly(self):
        intent = _make_intent(status=IntentStatus(phase=IntentPhase.READY))
        assert _intent_phase(intent) == "ready"

    def test_intent_phase_fallback(self):
        """Fallback for objects without status.phase."""

        class StubIntent:
            phase = "pending"

        assert _intent_phase(StubIntent()) == "pending"

    def test_intent_orphan_true(self):
        intent = _make_intent(metadata={"orphan": "true"})
        assert _intent_orphan(intent) is True

    def test_intent_orphan_false(self):
        intent = _make_intent(metadata={})
        assert _intent_orphan(intent) is False

    def test_detect_backend_drift_no_change(self):
        intent = _make_intent(backend={"backend_type": "hf", "max_length": 512})
        instance_config = {"backend_type": "hf", "max_length": 512}
        assert _detect_backend_drift(intent, instance_config) == []

    def test_detect_backend_drift_changed(self):
        intent = _make_intent(backend={"backend_type": "hf", "max_length": 1024})
        instance_config = {"backend_type": "hf", "max_length": 512}
        assert _detect_backend_drift(intent, instance_config) == ["max_length"]

    def test_detect_backend_drift_skips_identity_fields(self):
        """Identity/server fields (alias, model_source, etc.) are ignored."""
        intent = _make_intent(
            backend={"backend_type": "hf", "alias": "x", "model_source": "y"}
        )
        instance_config = {
            "backend_type": "hf",
            "alias": "different",
            "model_source": "z",
        }
        assert _detect_backend_drift(intent, instance_config) == []

    def test_detect_backend_drift_resolved_path_is_not_drift(self):
        """A bare filename in the spec that is the tail of the instance's
        resolved path is a match — the host stores resolve-time artifacts
        (e.g. mmproj) as absolute paths while the spec keeps the filename.
        Treating that as drift flags every replacement and traps the intent
        in a REPLACE-stop churn while the spec edit stays pending."""
        intent = _make_intent(
            backend={
                "backend_type": "llamacpp",
                "model_file": "*UD-Q8_K_XL*.gguf",
                "mmproj": "mmproj-BF16.gguf",
            }
        )
        instance_config = {
            "backend_type": "llamacpp",
            "model_file": "*UD-Q8_K_XL*.gguf",
            "mmproj": (
                "/opt/projects/models/hf--unsloth--Qwen3.6-35B-A3B-GGUF/"
                "mmproj-BF16.gguf"
            ),
        }
        assert _detect_backend_drift(intent, instance_config) == []

    def test_detect_backend_drift_resolved_glob_is_not_drift(self):
        """A glob resolves to a path too — the DSpark drafter's exact filename
        is not knowable from the intent, so the spec keeps the pattern."""
        intent = _make_intent(
            backend={
                "backend_type": "llamacpp",
                "spec_type": "draft-dspark",
                "spec_draft_model": "*DSpark*.gguf",
            }
        )
        instance_config = {
            "backend_type": "llamacpp",
            "spec_type": "draft-dspark",
            "spec_draft_model": (
                "/opt/projects/models/hf--org--Qwen3-4B-GGUF/Qwen3-4B-DSpark.gguf"
            ),
        }
        assert _detect_backend_drift(intent, instance_config) == []

    def test_detect_backend_drift_resolved_glob_real_change(self):
        """A resolved file the new pattern no longer matches is real drift."""
        intent = _make_intent(
            backend={"backend_type": "llamacpp", "spec_draft_model": "*DSpark*.gguf"}
        )
        instance_config = {
            "backend_type": "llamacpp",
            "spec_draft_model": (
                "/opt/projects/models/hf--org--Qwen3-4B-GGUF/Qwen3-4B-DFlash.gguf"
            ),
        }
        assert _detect_backend_drift(intent, instance_config) == ["spec_draft_model"]

    def test_detect_backend_drift_resolved_path_real_change(self):
        """A genuinely different resolved file still counts as drift."""
        intent = _make_intent(
            backend={"backend_type": "llamacpp", "mmproj": "mmproj-BF16.gguf"}
        )
        instance_config = {
            "backend_type": "llamacpp",
            "mmproj": (
                "/opt/projects/models/hf--unsloth--Qwen3.6-35B-A3B-GGUF/"
                "mmproj-Q4_K.gguf"
            ),
        }
        assert _detect_backend_drift(intent, instance_config) == ["mmproj"]

    def test_detect_backend_drift_non_path_mismatch_still_drift(self):
        """The path-tail normalization only applies to bare filenames —
        other field mismatches still read as drift."""
        intent = _make_intent(backend={"backend_type": "llamacpp", "ctx_size": 262144})
        instance_config = {"backend_type": "llamacpp", "ctx_size": 131072}
        assert _detect_backend_drift(intent, instance_config) == ["ctx_size"]


# C1 cross-service pin. Duplicated verbatim in
# apps/solar-host/tests/test_llamacpp_command.py — keep the two in step.
# The host rewrites chat_template_kwargs at its config boundary and control
# compares against the result, so the two coercions agreeing is what makes a
# canonicalized value read as "no drift" instead of churning the intent.
COERCION_PARITY_TABLE: list[tuple[object, object]] = [
    ({"a": "true", "b": ["false", "x"]}, {"a": True, "b": [False, "x"]}),
    ({"outer": {"enable_thinking": "True"}}, {"outer": {"enable_thinking": True}}),
    (" TRUE ", True),
    ("False", False),
    ("true", True),
    ("trueish", "trueish"),
    ("", ""),
    (["false"], [False]),
    ({}, {}),
    ([], []),
    (1, 1),
    (0, 0),
    (1.5, 1.5),
    (True, True),
    (None, None),
]


class TestBackendValueMatching:
    """C1 canonicalization-aware comparison.

    The host rewrites chat_template_kwargs into compact canonical JSON with
    real booleans and resolves mmproj to an absolute path; every
    representation the webui/API can produce must compare equal, and only a
    genuinely different value must read as drift.
    """

    def _detect(self, spec_value, inst_value) -> list[str]:
        intent = _make_intent(
            backend={"backend_type": "llamacpp", "chat_template_kwargs": spec_value}
        )
        instance_config = {
            "backend_type": "llamacpp",
            "chat_template_kwargs": inst_value,
        }
        return _detect_backend_drift(intent, instance_config)

    def test_dict_spec_versus_compact_json_instance(self):
        # spec {"enable_thinking": true} (dict) vs '{"enable_thinking":true}'
        assert self._detect({"enable_thinking": True}, '{"enable_thinking":true}') == []

    def test_spaced_json_spec_versus_compact_instance(self):
        assert (
            self._detect('{"enable_thinking": true}', '{"enable_thinking":true}') == []
        )

    def test_string_boolean_spec_versus_real_boolean_instance(self):
        assert (
            self._detect('{"enable_thinking": "true"}', '{"enable_thinking":true}')
            == []
        )

    def test_nested_boolean_coercion(self):
        spec = '{"thinking": {"enabled": "FALSE", "depth": 3}}'
        inst = '{"thinking":{"enabled":false,"depth":3}}'
        assert self._detect(spec, inst) == []

    def test_genuinely_different_kwargs_still_drift(self):
        assert self._detect(
            '{"enable_thinking": true}', '{"enable_thinking":false}'
        ) == ["chat_template_kwargs"]

    def test_malformed_json_falls_back_to_string_comparison(self):
        # Not JSON on one side -> string comparison -> drift
        assert self._detect("{not json", '{"enable_thinking":true}') == [
            "chat_template_kwargs"
        ]

    def _detect_field(self, field: str, spec_value, inst_value) -> list[str]:
        intent = _make_intent(backend={"backend_type": "llamacpp", field: spec_value})
        return _detect_backend_drift(
            intent, {"backend_type": "llamacpp", field: inst_value}
        )

    def test_string_boolean_versus_coerced_boolean_is_not_drift(self):
        """The host's Pydantic models coerce at the config boundary, which is
        C1's whole premise; excluding bare scalars from the JSON layer left
        exactly those coercions reading as drift."""
        assert self._detect_field("flash_attn", "true", True) == []
        assert self._detect_field("flash_attn", "false", False) == []
        assert self._detect_field("flash_attn", True, "true") == []

    def test_string_number_versus_coerced_number_is_not_drift(self):
        assert self._detect_field("ctx_size", "131072", 131072) == []
        assert self._detect_field("gpu_layers", 99, "99") == []
        assert self._detect_field("temperature", "0.7", 0.7) == []

    def test_a_genuinely_different_scalar_still_drifts(self):
        assert self._detect_field("flash_attn", "true", False) == ["flash_attn"]
        assert self._detect_field("ctx_size", "131072", 262144) == ["ctx_size"]

    def test_a_non_numeric_string_against_a_number_still_drifts(self):
        assert self._detect_field("ctx_size", "many", 262144) == ["ctx_size"]

    def test_mmproj_glob_spec_matches_resolved_path(self):
        intent = _make_intent(
            backend={"backend_type": "llamacpp", "mmproj": "*mmproj-BF16*.gguf"}
        )
        instance_config = {
            "backend_type": "llamacpp",
            "mmproj": "/opt/models/hf--x--y/mmproj-BF16-4bit.gguf",
        }
        assert _detect_backend_drift(intent, instance_config) == []

    def test_mmproj_glob_not_matching_resolved_basename_drifts(self):
        intent = _make_intent(
            backend={"backend_type": "llamacpp", "mmproj": "*mmproj-Q4*.gguf"}
        )
        instance_config = {
            "backend_type": "llamacpp",
            "mmproj": "/opt/models/hf--x--y/mmproj-BF16.gguf",
        }
        assert _detect_backend_drift(intent, instance_config) == ["mmproj"]

    def test_mmproj_relative_path_matches_resolved_path(self):
        intent = _make_intent(
            backend={"backend_type": "llamacpp", "mmproj": "sub/mmproj.gguf"}
        )
        instance_config = {
            "backend_type": "llamacpp",
            "mmproj": "/opt/models/hf--x--y/sub/mmproj.gguf",
        }
        assert _detect_backend_drift(intent, instance_config) == []

    def test_mmproj_glob_with_a_directory_component_matches(self):
        """Matching the pattern against the basename alone made any glob with a
        directory component unmatchable, so every replacement read as drift."""
        intent = _make_intent(
            backend={"backend_type": "llamacpp", "mmproj": "sub/*mmproj*.gguf"}
        )
        instance_config = {
            "backend_type": "llamacpp",
            "mmproj": "/opt/models/hf--x--y/sub/mmproj-BF16.gguf",
        }
        assert _detect_backend_drift(intent, instance_config) == []

    def test_mmproj_glob_with_a_wrong_directory_component_drifts(self):
        intent = _make_intent(
            backend={"backend_type": "llamacpp", "mmproj": "other/*mmproj*.gguf"}
        )
        instance_config = {
            "backend_type": "llamacpp",
            "mmproj": "/opt/models/hf--x--y/sub/mmproj-BF16.gguf",
        }
        assert _detect_backend_drift(intent, instance_config) == ["mmproj"]

    def test_dotfile_relative_path_is_not_stripped_to_its_stem(self):
        """``lstrip("./")`` takes a set of characters, so it ate the leading
        dot of a dotfile and then failed to match the resolved path."""
        intent = _make_intent(
            backend={"backend_type": "llamacpp", "mmproj": "./.hidden.gguf"}
        )
        instance_config = {
            "backend_type": "llamacpp",
            "mmproj": "/opt/models/hf--x--y/.hidden.gguf",
        }
        assert _detect_backend_drift(intent, instance_config) == []

    def test_dotfile_stem_alone_does_not_match_the_dotfile(self):
        intent = _make_intent(
            backend={"backend_type": "llamacpp", "mmproj": "hidden.gguf"}
        )
        instance_config = {
            "backend_type": "llamacpp",
            "mmproj": "/opt/models/hf--x--y/.hidden.gguf",
        }
        assert _detect_backend_drift(intent, instance_config) == ["mmproj"]


class TestPathMatchingIsScopedToResolvedPathKeys:
    """Layer 3 exists for fields the host resolves into an absolute path. Applied
    to every string field it reports *no* drift where there is some, which
    silently keeps a stale replica alive after a real config change — the mirror
    image of the churn C1 set out to fix.
    """

    def _detect_field(self, field: str, spec_value, inst_value) -> list[str]:
        intent = _make_intent(backend={"backend_type": "llamacpp", field: spec_value})
        return _detect_backend_drift(
            intent, {"backend_type": "llamacpp", field: inst_value}
        )

    def test_an_ot_regex_change_is_drift_even_when_it_globs(self):
        """``ot`` is a regex, and one routinely contains '*', '?' and '['. Read
        as a glob, the old spec happens to fnmatch the new instance value and
        the replica is left running the superseded override."""
        assert self._detect_field(
            "ot", "blk\\.[0-9]*\\.ffn.*=CPU", "blk.1.ffn=CUDA0"
        ) == ["ot"]

    def test_a_bare_star_does_not_match_every_value(self):
        assert self._detect_field("ot", "*", "blk.1.ffn=CPU") == ["ot"]

    def test_a_bare_star_mmproj_does_not_match_an_arbitrary_path(self):
        """Even on a path key, '*' matching any single-segment tail would call
        every resolved projector a match and mask a genuine change."""
        assert self._detect_field("mmproj", "*", "/opt/models/x/mmproj-BF16.gguf") == []
        # One segment of tail is all '*' can claim; a real edit still drifts.
        assert self._detect_field("mmproj", "*.bin", "/opt/models/x/mmproj.gguf") == [
            "mmproj"
        ]

    def test_a_relative_instance_value_does_not_tail_match(self):
        """Only a resolved absolute path is a tail-match target; two relative
        paths sharing a basename are different files."""
        assert self._detect_field("mmproj", "m.gguf", "other/m.gguf") == ["mmproj"]

    def test_a_non_path_field_sharing_a_suffix_is_still_drift(self):
        assert self._detect_field("chat_template_kwargs", "b/c", "/a/b/c") == [
            "chat_template_kwargs"
        ]

    def test_path_keys_still_match_their_resolved_paths(self):
        assert self._detect_field("model_file", "m.gguf", "/opt/models/x/m.gguf") == []
        assert (
            self._detect_field(
                "chat_template_file", "tpl.jinja", "/opt/models/x/tpl.jinja"
            )
            == []
        )

    def test_coerce_jsonish_matches_host_semantics(self):
        """Pins both of control's copies against the host's.

        Asserting only against control's own copy proves nothing about the
        agreement the comparison depends on: the host could change and this
        would still pass. The table below is duplicated verbatim in
        apps/solar-host/tests/test_llamacpp_command.py, which asserts the same
        rows against ``_coerce_template_kwargs`` — control's test env cannot
        import ``solar_host``, so identical tables either side is the pin.
        Change one implementation and the other suite fails.
        """
        from app.services.reconciliation import _coerce_jsonish
        from app.validation import _coerce_jsonish as _validation_coerce_jsonish

        for value, expected in COERCION_PARITY_TABLE:
            for name, fn in (
                ("reconciliation", _coerce_jsonish),
                ("validation", _validation_coerce_jsonish),
            ):
                result = fn(value)
                # Type too, not just equality: True == 1 in Python, so an
                # equality-only assertion cannot tell a coerced bool from the
                # int it must not become.
                assert result == expected and type(result) is type(expected), (
                    f"{name}._coerce_jsonish({value!r}) == {result!r}, "
                    f"expected {expected!r}"
                )


# ── Diff tests ─────────────────────────────────────────────────


class TestDiff:
    """Test the _diff method's action planning."""

    def test_noop_when_desired_matches_observed(self):
        """When replicas match, no actions needed."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=2)
        observed = _make_observed(
            managed=[
                _make_managed_instance("inst-1", host_id="h1"),
                _make_managed_instance("inst-2", host_id="h2"),
            ]
        )
        actions = reconciler._diff(intent, observed)
        assert actions == []

    def test_create_on_shortfall(self):
        """When observed < desired, create actions are generated from candidates."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=3)
        observed = _make_observed(
            managed=[_make_managed_instance("inst-1", host_id="h1")],
            hosts=[_HostStub(id="h2"), _HostStub(id="h3")],
            candidates=[
                (_HostStub(id="h2", name="h2"), _SnapshotStub("h2")),
                (_HostStub(id="h3", name="h3"), _SnapshotStub("h3")),
            ],
        )
        actions = reconciler._diff(intent, observed)
        creates = [a for a in actions if a.type == ActionType.CREATE]
        assert len(creates) == 2  # shortfall of 2

    def test_no_create_without_candidates(self):
        """When no candidates in observed, no CREATE actions even if shortfall."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=3)
        observed = _make_observed(
            managed=[_make_managed_instance("inst-1", host_id="h1")],
            hosts=[_HostStub(id="h2"), _HostStub(id="h3")],
        )
        actions = reconciler._diff(intent, observed)
        creates = [a for a in actions if a.type == ActionType.CREATE]
        assert len(creates) == 0  # candidates empty → no creates

    def test_stop_surplus(self):
        """When observed > desired, stop actions are generated."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=1)
        observed = _make_observed(
            managed=[
                _make_managed_instance("inst-1", host_id="h1"),
                _make_managed_instance("inst-2", host_id="h2"),
            ]
        )
        actions = reconciler._diff(intent, observed)
        stops = [a for a in actions if a.type == ActionType.STOP]
        assert len(stops) == 1

    def test_stop_surplus_least_loaded_first(self):
        """Tiebreak among same-age healthy replicas: least-loaded host first."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=1)
        snapshots = {
            "h1": _SnapshotStub("h1", running_instance_count=5),
            "h2": _SnapshotStub("h2", running_instance_count=1),
        }
        observed = _make_observed(
            managed=[
                _make_managed_instance("inst-1", host_id="h1"),
                _make_managed_instance("inst-2", host_id="h2"),
            ],
            snapshots=snapshots,
        )
        actions = reconciler._diff(intent, observed)
        stops = [a for a in actions if a.type == ActionType.STOP]
        assert len(stops) == 1
        assert stops[0].instance_id == "inst-2"  # least-loaded stopped first

    def test_replace_on_model_source_drift(self):
        """When instance model_source differs, replace action is generated."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=1, model_source="repo://test:v2")
        observed = _make_observed(
            managed=[
                _make_managed_instance("inst-1", model_source="repo://test:v1"),
            ]
        )
        actions = reconciler._diff(intent, observed)
        replaces = [a for a in actions if a.type == ActionType.REPLACE]
        assert len(replaces) == 1

    def test_replace_on_backend_drift(self):
        """When instance backend config differs, replace action is generated."""
        reconciler = Reconciler()
        intent = _make_intent(
            replicas=1,
            backend={"backend_type": "hf", "max_length": 1024},
        )
        observed = _make_observed(
            managed=[
                _make_managed_instance("inst-1", max_length=512),
            ]
        )
        actions = reconciler._diff(intent, observed)
        replaces = [a for a in actions if a.type == ActionType.REPLACE]
        assert len(replaces) == 1

    def test_recreate_on_failed_instance(self):
        """Failed instances trigger recreate actions."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=1)
        observed = _make_observed(
            managed=[
                _make_managed_instance("inst-1", status="failed"),
            ]
        )
        actions = reconciler._diff(intent, observed)
        recreates = [a for a in actions if a.type == ActionType.RECREATE]
        assert len(recreates) == 1

    def test_recreate_on_stopped_instance(self):
        """Stopped managed instances are drift → RECREATE (spec §8.2).

        D-017-9 exempted 'stopped' to stop /stop spam, but the spam came
        from _act RECREATE being stop-only. With restart-or-recreate
        semantics, a managed stopped instance (e.g. a migration target)
        must be restarted automatically.
        """
        reconciler = Reconciler()
        intent = _make_intent(replicas=1)
        observed = _make_observed(
            managed=[
                _make_managed_instance("inst-1", status="stopped"),
            ]
        )
        actions = reconciler._diff(intent, observed)
        recreates = [a for a in actions if a.type == ActionType.RECREATE]
        assert len(recreates) == 1

    def test_stop_all_on_delete(self):
        """Deleting intents get stop actions for all managed instances."""
        reconciler = Reconciler()
        intent = _make_intent(
            replicas=2,
            status=IntentStatus(phase=IntentPhase.DELETING),
        )
        observed = _make_observed(
            managed=[
                _make_managed_instance("inst-1", host_id="h1"),
                _make_managed_instance("inst-2", host_id="h2"),
            ]
        )
        actions = reconciler._diff(intent, observed)
        stops = [a for a in actions if a.type == ActionType.STOP]
        assert len(stops) == 2

    def test_disown_on_delete_orphan(self):
        """Deleting intents with orphan=true get DISOWN actions, not STOP."""
        reconciler = Reconciler()
        intent = _make_intent(
            replicas=2,
            metadata={"orphan": "true"},
            status=IntentStatus(phase=IntentPhase.DELETING),
        )
        observed = _make_observed(
            managed=[
                _make_managed_instance("inst-1", host_id="h1"),
                _make_managed_instance("inst-2", host_id="h2"),
            ]
        )
        actions = reconciler._diff(intent, observed)
        disowns = [a for a in actions if a.type == ActionType.DISOWN]
        stops = [a for a in actions if a.type == ActionType.STOP]
        assert len(disowns) == 2
        assert len(stops) == 0

    def test_stop_all_on_zero_replicas(self):
        """replicas=0 means stop all managed instances."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=0)
        observed = _make_observed(managed=[_make_managed_instance("inst-1")])
        actions = reconciler._diff(intent, observed)
        stops = [a for a in actions if a.type == ActionType.STOP]
        assert len(stops) == 1

    def test_actions_sorted_by_priority(self):
        """Actions are sorted: stops first (p0), recreate (p15), create (p50)."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=1)
        observed = _make_observed(
            managed=[
                _make_managed_instance("inst-1", host_id="h1", status="failed"),
                _make_managed_instance("inst-2", host_id="h2"),
            ],
            hosts=[_HostStub(id="h3")],
            candidates=[(_HostStub(id="h3", name="h3"), _SnapshotStub("h3"))],
        )
        # Surplus of 1 → stop, failed → recreate, shortfall → create
        actions = reconciler._diff(intent, observed)
        priorities = [a.priority for a in actions]
        assert priorities == sorted(
            priorities
        ), f"Expected sorted priorities, got {priorities}"

    def test_migrate_from_displacement(self):
        """When candidates are fewer than shortfall, MIGRATE actions are generated."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=2)
        observed = _make_observed(
            managed=[],
            hosts=[_HostStub(id="h1")],
            candidates=[
                (_HostStub(id="h1", name="h1"), _SnapshotStub("h1")),
            ],
            displaceable_map={
                "h2": [
                    {
                        "instance_id": "inst-ephemeral",
                        "config": {"alias": "other"},
                        "_priority": "ephemeral",
                    }
                ],
            },
        )
        actions = reconciler._diff(intent, observed)
        migrates = [a for a in actions if a.type == ActionType.MIGRATE]
        creates = [a for a in actions if a.type == ActionType.CREATE]
        assert len(creates) == 1  # 1 candidate used
        assert len(migrates) == 1  # 1 displacement needed for remaining shortfall


# ── Drain diff tests (S-043) ───────────────────────────────────


class TestDrainDiff:
    """Test evacuation planning for replicas on draining hosts."""

    def _draining_observed(self, *, candidates=None, managed=None, **kwargs):
        return _make_observed(
            managed=managed
            or [
                _make_managed_instance("inst-1", host_id="h1", host_name="h1"),
                _make_managed_instance("inst-2", host_id="h2", host_name="h2"),
            ],
            hosts=[
                _HostStub(id="h1", name="h1", drain_state="draining"),
                _HostStub(id="h2", name="h2"),
            ],
            candidates=candidates or [],
            **kwargs,
        )

    def test_evacuates_replica_to_best_candidate(self):
        reconciler = Reconciler()
        intent = _make_intent(replicas=2)
        observed = self._draining_observed(
            candidates=[(_HostStub(id="h3", name="h3"), _SnapshotStub("h3"))]
        )

        actions = reconciler._diff(intent, observed)
        evacuations = [a for a in actions if a.type == ActionType.EVACUATE]

        assert len(evacuations) == 1
        assert evacuations[0].instance_id == "inst-1"
        assert evacuations[0].host_id == "h1"
        assert evacuations[0].target_host_id == "h3"

    def test_evacuation_without_target_carries_the_stall_reason(self):
        """No candidate still emits the action so the stall is reported (§4.3)."""
        reconciler = Reconciler()
        intent = _make_intent(
            replicas=2,
            placement=PlacementConstraints(roles=["inference"], gpu_type="nvidia_cuda"),
            resources=ResourceRequirements(vram_gb=48.0),
        )
        observed = self._draining_observed()

        actions = reconciler._diff(intent, observed)
        evacuations = [a for a in actions if a.type == ActionType.EVACUATE]

        assert len(evacuations) == 1
        assert evacuations[0].target_host_id is None
        assert "No eligible host" in evacuations[0].reason
        assert "nvidia_cuda" in evacuations[0].reason
        assert "48.0 GB" in evacuations[0].reason

    def test_evacuation_runs_last(self):
        """Evacuation must not starve the intent's own convergence."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=3)
        observed = self._draining_observed(
            candidates=[(_HostStub(id="h3", name="h3"), _SnapshotStub("h3"))]
        )

        actions = reconciler._diff(intent, observed)

        assert actions[-1].type == ActionType.EVACUATE

    def test_replicas_on_healthy_hosts_are_left_alone(self):
        reconciler = Reconciler()
        intent = _make_intent(replicas=2)
        observed = self._draining_observed(
            candidates=[(_HostStub(id="h3", name="h3"), _SnapshotStub("h3"))]
        )

        actions = reconciler._diff(intent, observed)

        assert [a.instance_id for a in actions if a.type == ActionType.EVACUATE] == [
            "inst-1"
        ]

    def test_stopped_replica_is_stopped_instead_of_recreated(self):
        """RECREATE would rebuild it on the host being emptied (§4.2)."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=2)
        observed = self._draining_observed(
            managed=[
                _make_managed_instance(
                    "inst-1", host_id="h1", host_name="h1", status="failed"
                ),
                _make_managed_instance("inst-2", host_id="h2", host_name="h2"),
            ],
            candidates=[(_HostStub(id="h3", name="h3"), _SnapshotStub("h3"))],
        )

        actions = reconciler._diff(intent, observed)

        assert [a.type for a in actions if a.instance_id == "inst-1"] == [
            ActionType.STOP
        ]

    def test_surplus_replica_is_taken_from_the_draining_host(self):
        """Dropping it as surplus is cheaper than migrating it (§4.2)."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=1)
        observed = self._draining_observed()

        actions = reconciler._diff(intent, observed)
        stops = [a for a in actions if a.type == ActionType.STOP]

        assert [a.instance_id for a in stops] == ["inst-1"]
        # Already leaving; no need to also plan a migration for it
        assert not [a for a in actions if a.type == ActionType.EVACUATE]

    def test_drift_replacement_takes_precedence(self):
        """A REPLACE already relocates the replica through placement, which
        excludes the draining host, so the drain progresses without an
        evacuation."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=2, model_source="repo://test:v2")
        observed = self._draining_observed(
            managed=[
                _make_managed_instance(
                    "inst-1",
                    host_id="h1",
                    host_name="h1",
                    model_source="repo://test:v1",
                ),
                _make_managed_instance(
                    "inst-2",
                    host_id="h2",
                    host_name="h2",
                    model_source="repo://test:v2",
                ),
            ],
            candidates=[(_HostStub(id="h3", name="h3"), _SnapshotStub("h3"))],
        )

        actions = reconciler._diff(intent, observed)
        by_instance = [a.type for a in actions if a.instance_id == "inst-1"]

        assert ActionType.EVACUATE not in by_instance
        assert ActionType.REPLACE in by_instance

    def test_no_evacuation_without_draining_hosts(self):
        reconciler = Reconciler()
        intent = _make_intent(replicas=2)
        observed = _make_observed(
            managed=[
                _make_managed_instance("inst-1", host_id="h1"),
                _make_managed_instance("inst-2", host_id="h2"),
            ],
            hosts=[_HostStub(id="h1"), _HostStub(id="h2")],
        )

        actions = reconciler._diff(intent, observed)

        assert not [a for a in actions if a.type == ActionType.EVACUATE]


# ── Build instance config test ──────────────────────────────────


class TestBuildInstanceConfig:
    """Test _build_instance_config method."""

    def test_maps_fields_correctly(self):
        """Top-level: managed_by, intent_id, priority. Config: alias, source, backend.

        Per deployment-intent.md §6, managed_by/intent_id/priority are top-level
        fields on the Instance model, not nested inside config.
        """
        reconciler = Reconciler()
        intent = _make_intent(
            alias="iris-osl:110m",
            model_source="repo://iris-osl:v3",
            priority="production",
            backend={
                "backend_type": "huggingface_classification",
                "max_length": 512,
                "labels": ["osl"],
            },
        )
        host = _HostStub(id="h1")
        payload = reconciler._build_instance_config(intent, host)

        # Top-level fields
        assert payload["managed_by"] == "intent"
        assert payload["intent_id"] == "intent-001"
        assert payload["priority"] == "production"

        # Config fields
        assert payload["config"]["alias"] == "iris-osl:110m"
        assert payload["config"]["model_source"] == "repo://iris-osl:v3"
        assert payload["config"]["max_length"] == 512
        assert payload["config"]["backend_type"] == "huggingface_classification"

        # NOT inside config
        assert "managed_by" not in payload["config"]
        assert "intent_id" not in payload["config"]
        assert "priority" not in payload["config"]

    def test_copies_backend_runtime_params(self):
        """Backend params are copied to config, backend_type included."""
        reconciler = Reconciler()
        intent = _make_intent(
            backend={"backend_type": "llamacpp", "dtype": "float16"},
        )
        host = _HostStub(id="h1")
        config = reconciler._build_instance_config(intent, host)
        assert config["config"]["backend_type"] == "llamacpp"
        assert config["config"]["dtype"] == "float16"


# ── Backoff tests ──────────────────────────────────────────────


class TestBackoff:
    """Test exponential backoff logic."""

    def test_backoff_clear(self):
        reconciler = Reconciler()
        reconciler._backoff["test-id"] = {"failures": 3}
        reconciler._backoff_clear("test-id")
        assert "test-id" not in reconciler._backoff

    def test_backoff_active_after_failure(self):
        reconciler = Reconciler()
        reconciler._backoff_record_failure("test-id")
        assert reconciler._backoff_active("test-id") is True  # 10s backoff

    def test_backoff_not_active_for_unknown(self):
        reconciler = Reconciler()
        assert reconciler._backoff_active("unknown") is False

    def test_skip_when_backoff_active(self):
        """_reconcile_one returns early when backoff is active."""
        reconciler = Reconciler()
        intent = _make_intent()
        reconciler._backoff_record_failure(intent.id)

        with patch.object(reconciler, "_observe") as mock_observe:
            # Should not call _observe because backoff is active
            import asyncio

            asyncio.run(reconciler._reconcile_one(intent))
            mock_observe.assert_not_called()


# ── Integration tests ──────────────────────────────────────────


class TestReconcileOne:
    """Integration tests for the full observe→diff→act→status pipeline."""

    @pytest.mark.anyio
    async def test_noop_when_already_healthy(self):
        """When desired state matches, status is updated but no actions taken."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=2)
        managed = [
            _make_managed_instance("inst-1", host_id="h1"),
            _make_managed_instance("inst-2", host_id="h2"),
        ]

        with (
            patch.object(
                reconciler,
                "_observe",
                new=AsyncMock(
                    return_value=_make_observed(
                        managed=managed, gateway_aliases={"test-model"}
                    )
                ),
            ),
            patch.object(reconciler, "_update_status", new=AsyncMock()) as mock_status,
        ):
            await reconciler._reconcile_one(intent)
            mock_status.assert_called_once()

    @pytest.mark.anyio
    async def test_create_action_executed(self):
        """Shortfall triggers create action on eligible host."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=1)
        host = _HostStub(id="h1")

        observed = _make_observed(
            managed=[],
            hosts=[host],
            candidates=[(host, _SnapshotStub("h1"))],
        )

        with (
            patch.object(reconciler, "_observe", new=AsyncMock(return_value=observed)),
            patch.object(
                reconciler,
                "_act",
                new=AsyncMock(return_value={"instance_id": "new-inst"}),
            ) as mock_act,
            patch.object(reconciler, "_update_status", new=AsyncMock()) as mock_status,
        ):
            await reconciler._reconcile_one(intent)
            mock_act.assert_called_once()
            mock_status.assert_called_once()

    @pytest.mark.anyio
    async def test_error_reported_in_status(self):
        """When action fails, last_error is populated."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=1)
        host = _HostStub(id="h1")

        observed = _make_observed(
            managed=[],
            hosts=[host],
            candidates=[(host, _SnapshotStub("h1"))],
        )

        with (
            patch.object(reconciler, "_observe", new=AsyncMock(return_value=observed)),
            patch.object(
                reconciler,
                "_act",
                new=AsyncMock(side_effect=RuntimeError("host unreachable")),
            ),
            patch.object(reconciler, "_update_status", new=AsyncMock()) as mock_status,
        ):
            await reconciler._reconcile_one(intent)

            # Verify last_error was passed to _update_status
            call_kwargs = mock_status.call_args
            last_error = call_kwargs[1].get("last_error")
            assert last_error is not None
            assert last_error["code"] == "RuntimeError"
            assert "host unreachable" in last_error["message"]

    @pytest.mark.anyio
    async def test_backoff_recorded_on_failure(self):
        """After a failure, backoff is set."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=1)
        host = _HostStub(id="h1")

        observed = _make_observed(
            managed=[],
            hosts=[host],
            candidates=[(host, _SnapshotStub("h1"))],
        )

        with (
            patch.object(reconciler, "_observe", new=AsyncMock(return_value=observed)),
            patch.object(
                reconciler,
                "_act",
                new=AsyncMock(side_effect=RuntimeError("fail")),
            ),
            patch.object(reconciler, "_update_status", new=AsyncMock()),
        ):
            await reconciler._reconcile_one(intent)

        assert reconciler._backoff_active(intent.id) is True
        assert reconciler._backoff[intent.id]["failures"] == 1


# ── _act tests ──────────────────────────────────────────────────


class TestActRecreate:
    """_act RECREATE: restart-or-recreate with backoff (§8.2)."""

    @pytest.mark.anyio
    async def test_recreate_restarts_failed_instance(self):
        """RECREATE restarts the instance in place (spec §8.2)."""
        reconciler = Reconciler()
        host = _HostStub(id="h1")
        action = Action(
            type=ActionType.RECREATE,
            intent_id="intent-001",
            alias="test-model",
            host_id="h1",
            instance_id="inst-1",
            reason="Instance stopped, recreating",
        )
        with (
            patch("app.database.hosts.host_db") as mock_db,
            patch.object(reconciler, "_start_instance", new=AsyncMock()) as mock_start,
        ):
            mock_db.get_host = AsyncMock(return_value=host)
            result = await reconciler._act(_make_intent(), action)
            mock_start.assert_awaited_once_with(host, "inst-1")
            assert result is None  # restart path returns None (next tick is no-op)

    @pytest.mark.anyio
    async def test_recreate_deletes_and_raises_when_restart_fails(self):
        """Restart failure → delete broken replica + raise (backoff recorded)."""
        from fastapi import HTTPException

        reconciler = Reconciler()
        host = _HostStub(id="h1")
        action = Action(
            type=ActionType.RECREATE,
            intent_id="intent-001",
            alias="test-model",
            host_id="h1",
            instance_id="inst-1",
            reason="Instance failed, recreating",
        )
        with (
            patch("app.database.hosts.host_db") as mock_db,
            patch.object(
                reconciler,
                "_start_instance",
                new=AsyncMock(
                    side_effect=HTTPException(status_code=502, detail="boom")
                ),
            ),
            patch.object(
                reconciler, "_delete_instance", new=AsyncMock()
            ) as mock_delete,
        ):
            mock_db.get_host = AsyncMock(return_value=host)
            with pytest.raises(HTTPException):
                await reconciler._act(_make_intent(), action)
            mock_delete.assert_awaited_once_with(host, "inst-1")

    @pytest.mark.anyio
    async def test_recreate_restart_failure_records_backoff(self):
        """Failed recreate (via _reconcile_one) engages exponential backoff."""
        from fastapi import HTTPException

        reconciler = Reconciler()
        intent = _make_intent(replicas=1)
        managed = [_make_managed_instance("inst-1", status="failed")]
        observed = _make_observed(managed=managed)

        async def boom(intent_, action):
            raise HTTPException(status_code=502, detail="start failed")

        with (
            patch.object(reconciler, "_observe", new=AsyncMock(return_value=observed)),
            patch.object(reconciler, "_act", new=AsyncMock(side_effect=boom)),
            patch.object(reconciler, "_update_status", new=AsyncMock()),
        ):
            await reconciler._reconcile_one(intent)
        assert reconciler._backoff_active(intent.id) is True


class TestActMigrate:
    """_act MIGRATE: stop fallback when no target exists (§8.5)."""

    @pytest.mark.anyio
    async def test_migrate_no_target_ephemeral_falls_back_to_stop(self):
        """No migration target + ephemeral instance → stop+delete fallback."""
        reconciler = Reconciler()
        host = _HostStub(id="h1")
        action = Action(
            type=ActionType.MIGRATE,
            intent_id="intent-001",
            alias="other-alias",
            host_id="h1",
            instance_id="inst-1",
            reason="Displacing other-alias (ephemeral) to free capacity",
        )
        with (
            patch("app.database.hosts.host_db") as mock_db,
            patch("app.redis_state.host_store") as mock_store,
            patch(
                "app.services.migration.stop_source_instance", new=AsyncMock()
            ) as mock_stop,
            patch.object(
                reconciler, "_delete_instance", new=AsyncMock()
            ) as mock_delete,
        ):
            mock_db.get_host = AsyncMock(return_value=host)
            mock_db.get_all_hosts = AsyncMock(return_value=[])
            with patch(
                "app.services.placement.find_candidates",
                new=AsyncMock(return_value=[]),
            ):
                mock_store.get_host_instances = AsyncMock(
                    return_value=[{"instance_id": "inst-1", "priority": "ephemeral"}]
                )
                await reconciler._act(_make_intent(), action)
            mock_stop.assert_awaited_once_with(host, "inst-1")
            mock_delete.assert_awaited_once_with(host, "inst-1")

    @pytest.mark.anyio
    async def test_migrate_no_target_staging_not_stopped(self):
        """No migration target + staging instance → no stop fallback."""
        reconciler = Reconciler()
        host = _HostStub(id="h1")
        action = Action(
            type=ActionType.MIGRATE,
            intent_id="intent-001",
            alias="other-alias",
            host_id="h1",
            instance_id="inst-1",
            reason="Displacing other-alias (staging) to free capacity",
        )
        with (
            patch("app.database.hosts.host_db") as mock_db,
            patch("app.redis_state.host_store") as mock_store,
            patch(
                "app.services.migration.stop_source_instance", new=AsyncMock()
            ) as mock_stop,
            patch.object(
                reconciler, "_delete_instance", new=AsyncMock()
            ) as mock_delete,
        ):
            mock_db.get_host = AsyncMock(return_value=host)
            mock_db.get_all_hosts = AsyncMock(return_value=[])
            with patch(
                "app.services.placement.find_candidates",
                new=AsyncMock(return_value=[]),
            ):
                mock_store.get_host_instances = AsyncMock(
                    return_value=[{"instance_id": "inst-1", "priority": "staging"}]
                )
                result = await reconciler._act(_make_intent(), action)
            assert result is None
            mock_stop.assert_not_called()
            mock_delete.assert_not_called()


class TestSpecEditRollout:
    """An edited spec (S-044) rolls out under the intent's strategy."""

    def test_backend_drift_initiates_a_rollout(self):
        """A config-only edit is a rollout, not a no-op.

        The replica still carries the intent's model_source, so the strategy
        can only recognise it from the REPLACE the diff planned.
        """
        reconciler = Reconciler()
        intent = _make_intent(
            replicas=1,
            strategy="rolling",
            backend={"backend_type": "hf", "max_length": 1024},
        )
        observed = _make_observed(
            managed=[_make_managed_instance("inst-1", max_length=512)]
        )
        actions = reconciler._diff(intent, observed)

        progress = reconciler._maybe_initiate_strategy(intent, observed, actions)

        assert progress is not None
        assert progress["strategy"] == "rolling"
        assert progress["drifted_instance_ids"] == ["inst-1"]

    @pytest.mark.anyio
    async def test_replace_retires_the_replica_it_stops(self):
        """A stopped replica still counts, so REPLACE has to delete it too.

        Otherwise observed_replicas stays at the desired count with nothing
        serving: no CREATE for the replacement, and the RECREATE that would
        restart it is suppressed by this REPLACE.
        """
        reconciler = Reconciler()
        host = _HostStub(id="h1", name="h1")
        action = Action(
            type=ActionType.REPLACE,
            intent_id="intent-001",
            alias="test-model",
            host_id="h1",
            instance_id="inst-1",
            reason="backend config drift",
        )

        with (
            patch("app.database.hosts.host_db") as mock_db,
            patch(
                "app.services.migration.stop_source_instance", new=AsyncMock()
            ) as mock_stop,
            patch.object(
                reconciler, "_delete_instance", new=AsyncMock()
            ) as mock_delete,
        ):
            mock_db.get_host = AsyncMock(return_value=host)
            await reconciler._act(_make_intent(), action)

        mock_stop.assert_awaited_once_with(host, "inst-1")
        mock_delete.assert_awaited_once_with(host, "inst-1")


class TestSpecSettled:
    """A pending spec change is only settled when it could be checked."""

    def test_unreadable_replica_config_keeps_the_edit_pending(self):
        """Not being able to compare is not the same as finding no drift.

        Backend fields live in the replica's real configuration on the host;
        the cached view carries almost none of them. Settling here would clear
        the marker and lose the edit, leaving the replica on the old config
        while the intent reported itself up to date.
        """
        observed = _make_observed(
            managed=[{"instance_id": "i1", "_full_config_unknown": True}]
        )

        assert _spec_settled(observed, []) is False

    def test_settles_when_every_replica_matches(self):
        observed = _make_observed(
            managed=[{"instance_id": "i1", "_full_config": {"max_length": 1024}}]
        )

        assert _spec_settled(observed, []) is True

    def test_does_not_settle_while_a_replica_still_needs_replacing(self):
        observed = _make_observed(managed=[{"instance_id": "i1", "_full_config": {}}])
        actions = [
            Action(
                type=ActionType.REPLACE,
                intent_id="intent-001",
                alias="test-model",
                host_id="h1",
                instance_id="i1",
                reason="backend config drift",
            )
        ]

        assert _spec_settled(observed, actions) is False


class TestCreateThatCannotStart:
    """A replacement that will not start must leave nothing behind (§11.5)."""

    @pytest.mark.anyio
    async def test_failed_start_removes_the_instance_and_raises(self):
        """One dead instance per retry would otherwise pile up on the host.

        Each would also still count as an observed replica of the intent,
        so the shortfall never shows the alias is down.
        """
        reconciler = Reconciler()
        host = _HostStub(id="h1", name="h1")
        action = Action(
            type=ActionType.CREATE,
            intent_id="intent-001",
            alias="test-model",
            host_id="h1",
            reason="shortfall 1/1",
        )

        with (
            patch("app.database.hosts.host_db") as mock_db,
            patch(
                "app.services.migration.create_instance_on_host",
                new=AsyncMock(return_value={"instance": {"id": "inst-new"}}),
            ),
            patch.object(
                reconciler,
                "_start_instance",
                new=AsyncMock(side_effect=HTTPException(status_code=502, detail="oom")),
            ),
            patch.object(
                reconciler, "_delete_instance", new=AsyncMock()
            ) as mock_delete,
        ):
            mock_db.get_host = AsyncMock(return_value=host)

            with pytest.raises(HTTPException):
                await reconciler._act(_make_intent(), action)

        mock_delete.assert_awaited_once_with(host, "inst-new")

    @pytest.mark.anyio
    async def test_start_with_no_answer_keeps_the_instance(self):
        """A start the host never answered is not a start that failed.

        The host's start blocks while it launches the server, so a timeout
        under load says nothing — and the replica is usually coming up.
        Deleting it would destroy a healthy replica mid-start.
        """
        reconciler = Reconciler()
        host = _HostStub(id="h1", name="h1")
        action = Action(
            type=ActionType.CREATE,
            intent_id="intent-001",
            alias="test-model",
            host_id="h1",
            reason="shortfall 1/1",
        )

        with (
            patch("app.database.hosts.host_db") as mock_db,
            patch(
                "app.services.migration.create_instance_on_host",
                new=AsyncMock(return_value={"instance": {"id": "inst-new"}}),
            ),
            patch.object(
                reconciler,
                "_start_instance",
                new=AsyncMock(side_effect=StartOutcomeUnknown("timeout")),
            ),
            patch.object(
                reconciler, "_delete_instance", new=AsyncMock()
            ) as mock_delete,
        ):
            mock_db.get_host = AsyncMock(return_value=host)

            with pytest.raises(StartOutcomeUnknown):
                await reconciler._act(_make_intent(), action)

        mock_delete.assert_not_awaited()

    @pytest.mark.anyio
    async def test_recreate_keeps_a_replica_whose_restart_had_no_answer(self):
        reconciler = Reconciler()
        host = _HostStub(id="h1", name="h1")
        action = Action(
            type=ActionType.RECREATE,
            intent_id="intent-001",
            alias="test-model",
            host_id="h1",
            instance_id="inst-1",
            reason="Instance stopped, recreating",
        )

        with (
            patch("app.database.hosts.host_db") as mock_db,
            patch.object(
                reconciler,
                "_start_instance",
                new=AsyncMock(side_effect=StartOutcomeUnknown("timeout")),
            ),
            patch.object(
                reconciler, "_delete_instance", new=AsyncMock()
            ) as mock_delete,
        ):
            mock_db.get_host = AsyncMock(return_value=host)

            with pytest.raises(StartOutcomeUnknown):
                await reconciler._act(_make_intent(), action)

        mock_delete.assert_not_awaited()

    @pytest.mark.anyio
    async def test_rollout_stays_put_when_the_start_outcome_is_unknown(self):
        """Moving hosts now could leave two replacements, not one."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=1, strategy="rolling")
        host_b = _HostStub(id="h2", name="h2")
        observed = _make_observed(
            managed=[_make_managed_instance("i1", host_id="h1", max_length=512)],
            candidates=[(host_b, _SnapshotStub(host_id="h2"))],
        )
        progress = {
            "strategy": "rolling",
            "target_model_source": intent.model_source,
            "drifted_instance_ids": ["i1"],
            "phase": "creating_replacement",
            "step": "1/1",
            "updated": 0,
            "in_progress": 1,
            "failed": 0,
            "current_host_id": "h1",
            "current_instance_id": None,
            "pending_hosts": [],
            "failed_hosts": [],
            "started_at": "2026-01-01T00:00:00+00:00",
        }

        with (
            patch.object(
                reconciler,
                "_act",
                new=AsyncMock(side_effect=StartOutcomeUnknown("timeout")),
            ),
            patch.object(reconciler, "_update_status", new=AsyncMock()) as mock_status,
        ):
            await reconciler._continue_strategy(intent, observed, progress)

        new_progress = mock_status.await_args.kwargs["strategy_progress"]
        assert new_progress["current_host_id"] == "h1"
        assert new_progress["failed_hosts"] == []
        assert new_progress["failed"] == 1

    @pytest.mark.anyio
    async def test_rollout_moves_off_the_host_that_could_not_take_it(self):
        """The strategy must consume the failure, not retry the same host."""
        reconciler = Reconciler()
        intent = _make_intent(replicas=1, strategy="rolling")
        host_b = _HostStub(id="h2", name="h2")
        observed = _make_observed(
            managed=[_make_managed_instance("i1", host_id="h1", max_length=512)],
            candidates=[(host_b, _SnapshotStub(host_id="h2"))],
        )
        progress = {
            "strategy": "rolling",
            "target_model_source": intent.model_source,
            "drifted_instance_ids": ["i1"],
            "phase": "creating_replacement",
            "step": "1/1",
            "updated": 0,
            "in_progress": 1,
            "failed": 0,
            "current_host_id": "h1",
            "current_instance_id": None,
            "pending_hosts": [],
            "failed_hosts": [],
            "started_at": "2026-01-01T00:00:00+00:00",
        }

        with (
            patch.object(
                reconciler,
                "_act",
                new=AsyncMock(side_effect=HTTPException(status_code=502, detail="oom")),
            ),
            patch.object(reconciler, "_update_status", new=AsyncMock()) as mock_status,
        ):
            await reconciler._continue_strategy(intent, observed, progress)

        new_progress = mock_status.await_args.kwargs["strategy_progress"]
        assert new_progress["current_host_id"] == "h2"
        assert new_progress["failed_hosts"] == ["h1"]
        assert mock_status.await_args.kwargs["last_error"]["message"]
        # A retry on another host is progress, so it is not paced by backoff.
        assert reconciler._backoff_active(intent.id) is False

    @pytest.mark.anyio
    async def test_rollout_holds_and_backs_off_when_no_host_is_left(self):
        reconciler = Reconciler()
        intent = _make_intent(replicas=1, strategy="rolling")
        observed = _make_observed(
            managed=[_make_managed_instance("i1", host_id="h1", max_length=512)]
        )
        progress = {
            "strategy": "rolling",
            "target_model_source": intent.model_source,
            "drifted_instance_ids": ["i1"],
            "phase": "creating_replacement",
            "step": "1/1",
            "updated": 0,
            "in_progress": 1,
            "failed": 0,
            "current_host_id": "h1",
            "current_instance_id": None,
            "pending_hosts": [],
            "failed_hosts": [],
            "started_at": "2026-01-01T00:00:00+00:00",
        }

        with (
            patch.object(
                reconciler,
                "_act",
                new=AsyncMock(side_effect=HTTPException(status_code=502, detail="oom")),
            ),
            patch.object(reconciler, "_update_status", new=AsyncMock()) as mock_status,
        ):
            await reconciler._continue_strategy(intent, observed, progress)

        new_progress = mock_status.await_args.kwargs["strategy_progress"]
        assert new_progress["phase"] == "failed"
        assert "no other host" in new_progress["message"]
        assert reconciler._backoff_active(intent.id) is True


class TestActEvacuate:
    """_act EVACUATE: drain evacuation runs create-then-stop and leaves
    the source host empty (S-043 §4.2, S-057)."""

    def _action(self, target_host_id: str | None = "h2") -> Action:
        return Action(
            type=ActionType.EVACUATE,
            intent_id="intent-001",
            alias="test-model",
            host_id="h1",
            host_name="h1",
            instance_id="inst-1",
            target_host_id=target_host_id,
            target_host_name="h2" if target_host_id else None,
            reason="host draining → migrate to h2",
        )

    @pytest.mark.anyio
    async def test_delegates_to_execute_evacuation_and_clears_stall(self):
        """The executor delegates to execute_evacuation (create-then-stop);
        the source deletion happens inside it, not here."""
        reconciler = Reconciler()
        migration = SimpleNamespace(
            migration_id="mig-1", status="completed", error=None
        )

        with (
            patch(
                "app.services.migration.execute_evacuation",
                new=AsyncMock(return_value=migration),
            ) as mock_evacuate,
            patch("app.services.drain.clear_stall", new=AsyncMock()) as mock_clear,
            patch("app.services.drain.record_stall", new=AsyncMock()) as mock_stall,
            patch.object(
                reconciler, "_delete_instance", new=AsyncMock()
            ) as mock_delete,
        ):
            result = await reconciler._act(_make_intent(), self._action())

        assert result == {"migration_id": "mig-1", "status": "completed"}
        assert mock_evacuate.await_args.kwargs["target_host_id"] == "h2"
        assert mock_evacuate.await_args.kwargs["source_host_id"] == "h1"
        # execute_evacuation stops and deletes the source itself.
        mock_delete.assert_not_called()
        mock_clear.assert_awaited_once_with("h1", "inst-1")
        mock_stall.assert_not_called()

    @pytest.mark.anyio
    async def test_no_target_records_stall_without_touching_the_replica(self):
        """A stall is not a failure: the replica keeps serving (§4.3)."""
        reconciler = Reconciler()

        with (
            patch(
                "app.services.migration.execute_evacuation", new=AsyncMock()
            ) as mock_evacuate,
            patch("app.services.drain.record_stall", new=AsyncMock()) as mock_stall,
            patch.object(
                reconciler, "_delete_instance", new=AsyncMock()
            ) as mock_delete,
        ):
            result = await reconciler._act(
                _make_intent(), self._action(target_host_id=None)
            )

        assert result is None
        mock_evacuate.assert_not_called()
        mock_delete.assert_not_called()
        mock_stall.assert_awaited_once()

    @pytest.mark.anyio
    async def test_incomplete_evacuation_stalls_without_raising_or_backoff(self):
        """A failed evacuation is a stall, not an error: it re-evaluates
        every tick (§4.3) and must not accumulate exponential backoff."""
        reconciler = Reconciler()
        migration = SimpleNamespace(
            migration_id="mig-1", status="failed", error="pull failed"
        )
        intent = _make_intent()

        with (
            patch(
                "app.services.migration.execute_evacuation",
                new=AsyncMock(return_value=migration),
            ),
            patch("app.services.drain.record_stall", new=AsyncMock()) as mock_stall,
            patch.object(
                reconciler, "_delete_instance", new=AsyncMock()
            ) as mock_delete,
        ):
            result = await reconciler._act(intent, self._action())

        assert result is None
        mock_delete.assert_not_called()
        assert "Evacuation failed: pull failed" in mock_stall.await_args.args[2]
        assert reconciler._backoff_active(intent.id) is False

    @pytest.mark.anyio
    async def test_unexpected_exception_stalls_without_raising(self):
        """Even an unexpected exception records a stall and returns; the
        drain re-evaluates next tick instead of backing off."""
        reconciler = Reconciler()
        intent = _make_intent()

        with (
            patch(
                "app.services.migration.execute_evacuation",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch("app.services.drain.record_stall", new=AsyncMock()) as mock_stall,
            patch.object(
                reconciler, "_delete_instance", new=AsyncMock()
            ) as mock_delete,
        ):
            result = await reconciler._act(intent, self._action())

        assert result is None
        mock_delete.assert_not_called()
        mock_stall.assert_awaited_once_with(
            "h1", "inst-1", "Evacuation raised an unexpected error"
        )
        assert reconciler._backoff_active(intent.id) is False


class TestUpdateStatusConditions:
    """Status conditions emitted by _update_status (§10.3)."""

    @pytest.mark.anyio
    async def test_update_status_emits_degraded_condition(self):
        """DEGRADED phase → Degraded condition, not Progressing."""
        reconciler = Reconciler()
        intent = _make_intent(
            status=IntentStatus(
                phase=IntentPhase.DEGRADED,
                reconcile=ReconcileState.IN_PROGRESS,
                desired_replicas=2,
            )
        )
        observed = _make_observed(
            managed=[_make_managed_instance("inst-1", status="running")],
            gateway_aliases={"test-model"},
        )
        with patch("app.database.intents.intent_db") as mock_db:
            mock_db.update_status = AsyncMock()
            await reconciler._update_status(intent, observed)
        _, kwargs = mock_db.update_status.call_args
        status_json = kwargs["status_json"]
        types = {c["type"] for c in status_json["conditions"]}
        assert "Degraded" in types
        assert "Progressing" not in types


class TestPerIntentStateIsPruned:
    """Every per-intent dict on the reconciler is only ever written, so a
    deleted intent's entries used to survive for the lifetime of the process.
    The leak is slow, but it is unbounded — a long-lived control instance
    accumulates one entry per intent ever created."""

    def _seeded(self) -> Reconciler:
        reconciler = Reconciler()
        for intent_id in ("live", "gone"):
            reconciler._backoff[intent_id] = {"failures": 1, "next_retry_at": "x"}
            reconciler._settle_until[intent_id] = time.monotonic() + 60
            reconciler._fleet_violations_logged[intent_id] = "spec-1"
            reconciler._config_cache_spec[intent_id] = "spec-1"
            reconciler._config_cache[(intent_id, "inst-1", "spec-1")] = {"a": 1}
        return reconciler

    def test_drops_every_dict_for_an_intent_that_is_gone(self):
        reconciler = self._seeded()

        reconciler._prune_intent_state({"live"})

        assert set(reconciler._backoff) == {"live"}
        assert set(reconciler._settle_until) == {"live"}
        assert set(reconciler._fleet_violations_logged) == {"live"}
        assert set(reconciler._config_cache_spec) == {"live"}
        assert {k[0] for k in reconciler._config_cache} == {"live"}

    def test_keeps_state_for_an_intent_still_in_the_listing(self):
        reconciler = self._seeded()

        reconciler._prune_intent_state({"live", "gone"})

        assert set(reconciler._backoff) == {"live", "gone"}
        assert set(reconciler._config_cache_spec) == {"live", "gone"}

    def test_displace_cooldown_is_pruned_by_expiry_not_by_intent_id(self):
        """It is keyed by *instance* id, so pruning it against intent ids would
        drop every entry on the first tick and re-enable the thrash the
        cooldown exists to prevent."""
        reconciler = Reconciler()
        now = time.monotonic()
        reconciler._displace_cooldown["inst-live"] = now + 60
        reconciler._displace_cooldown["inst-expired"] = now - 1

        reconciler._prune_intent_state({"live"})

        assert set(reconciler._displace_cooldown) == {"inst-live"}
