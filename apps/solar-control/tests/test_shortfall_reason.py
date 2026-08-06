"""C3 _shortfall_reason: unplaceable intents get a specific message instead
of only the generic 'desired replicas cannot all be made ready'."""

from app.models.intent import (
    PlacementConstraints,
    ResourceRequirements,
)
from app.services.reconciliation import _shortfall_reason

from test_reconciliation import _HostStub, _SnapshotStub, _make_intent, _make_observed


def test_no_reason_when_fulfilled():
    intent = _make_intent(replicas=1)
    observed = _make_observed(
        managed=[{"instance_id": "i1", "_host_id": "h1"}],
        hosts=[_HostStub(id="h1")],
    )
    assert _shortfall_reason(intent, observed) is None


def test_no_host_matches_gpu_type():
    intent = _make_intent(
        replicas=2,
        placement=PlacementConstraints(gpu_type="apple_mps"),
    )
    observed = _make_observed(
        hosts=[_HostStub(id="h1", gpu_type="nvidia_cuda")],
    )
    reason = _shortfall_reason(intent, observed)
    assert reason == "no host matches gpu_type=apple_mps"


def test_no_host_matches_roles():
    intent = _make_intent(
        replicas=2, placement=PlacementConstraints(roles=["training"])
    )
    observed = _make_observed(hosts=[_HostStub(id="h1")])
    assert _shortfall_reason(intent, observed) == "no host matches roles ['training']"


def test_host_allow_smaller_than_replicas():
    intent = _make_intent(
        replicas=3,
        placement=PlacementConstraints(host_allow=["h1"]),
    )
    observed = _make_observed(hosts=[_HostStub(id="h1")])
    reason = _shortfall_reason(intent, observed)
    assert reason == "host_allow names 1 host(s), 3 replicas requested"


def test_all_eligible_hosts_draining():
    intent = _make_intent(replicas=2)
    observed = _make_observed(
        hosts=[
            _HostStub(id="h1", drain_state="draining"),
            _HostStub(id="h2", drain_state="draining"),
        ],
    )
    reason = _shortfall_reason(intent, observed)
    assert reason == "all 2 eligible host(s) are draining"


def test_vram_above_largest_available():
    intent = _make_intent(
        replicas=2,
        resources=ResourceRequirements(vram_gb=24.0),
    )
    observed = _make_observed(
        hosts=[_HostStub(id="h1")],
        snapshots={"h1": _SnapshotStub(host_id="h1", vram_available_gb=16.0)},
    )
    reason = _shortfall_reason(intent, observed)
    assert reason is not None
    assert "needs 24 GB VRAM" in reason
    assert "16 GB" in reason


def test_generic_fallback_when_no_specific_cause():
    """A full-but-unplaceable fleet (e.g. one-replica-per-host saturation)
    falls back to None so the generic message stays."""
    intent = _make_intent(replicas=2)
    observed = _make_observed(
        hosts=[_HostStub(id="h1")],
        snapshots={"h1": _SnapshotStub(host_id="h1", vram_available_gb=100.0)},
    )
    assert _shortfall_reason(intent, observed) is None
