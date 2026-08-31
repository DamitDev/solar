"""C3 _shortfall_reason: unplaceable intents get a specific message instead
of only the generic 'desired replicas cannot all be made ready'."""

from test_placement import _gpu_snap
from test_reconciliation import _HostStub, _make_intent, _make_observed, _SnapshotStub

from app.models.intent import (
    PlacementConstraints,
    ResourceRequirements,
)
from app.services.reconciliation import _shortfall_reason


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


def test_a_stored_gpu_type_alias_matches_the_canonical_host_token():
    """Normalization runs on write, so a row stored before it landed can still
    hold an alias. Reading it raw made the fleet validator (which normalizes)
    call the intent placeable while the reconciler matched nothing."""
    intent = _make_intent(replicas=1, placement=PlacementConstraints(gpu_type="mps"))
    observed = _make_observed(
        hosts=[_HostStub(id="h1", gpu_type="apple_mps")],
        snapshots={"h1": _SnapshotStub(host_id="h1", vram_available_gb=100.0)},
    )
    assert _shortfall_reason(intent, observed) is None


def test_no_host_supports_the_backend():
    """SGLang pins gpu_type, so a missing install must not be reported as a
    gpu_type mismatch — the operator would go looking at the wrong thing."""
    intent = _make_intent(
        replicas=1,
        backend={"backend_type": "sglang"},
        placement=PlacementConstraints(gpu_type="nvidia_cuda"),
    )
    observed = _make_observed(
        hosts=[
            _HostStub(id="h1", gpu_type="nvidia_cuda", supported_backends=["llamacpp"])
        ],
        snapshots={"h1": _SnapshotStub(host_id="h1", vram_available_gb=100.0)},
    )
    assert _shortfall_reason(intent, observed) == "no host supports backend=sglang"


def test_a_host_advertising_the_backend_is_not_reported():
    intent = _make_intent(
        replicas=1,
        backend={"backend_type": "sglang"},
        placement=PlacementConstraints(gpu_type="nvidia_cuda"),
    )
    observed = _make_observed(
        hosts=[
            _HostStub(id="h1", gpu_type="nvidia_cuda", supported_backends=["sglang"])
        ],
        snapshots={"h1": _SnapshotStub(host_id="h1", vram_available_gb=100.0)},
    )
    assert _shortfall_reason(intent, observed) is None


def test_an_unknown_gpu_type_still_matches_nothing():
    intent = _make_intent(replicas=1, placement=PlacementConstraints(gpu_type="rocm"))
    observed = _make_observed(hosts=[_HostStub(id="h1", gpu_type="nvidia_cuda")])
    assert _shortfall_reason(intent, observed) == "no host matches gpu_type=rocm"


def test_gpu_count_one_shortfall_reported_per_device():
    """gpu_count=1 on per-GPU hosts is bin-packed per device, not against
    the aggregate number: a 50 GB single-GPU intent against 3x20 GB
    devices must produce a per-device reason. (The pre-fix gate on
    ``gpu_count > 1`` fell through to the aggregate 60 GB, which is
    exactly the misleading number S-058 exists to eliminate — and returned
    None.)"""
    intent = _make_intent(
        replicas=2,
        resources=ResourceRequirements(vram_gb=50.0, gpu_count=1),
    )
    observed = _make_observed(
        hosts=[_HostStub(id="h1", gpu_type="nvidia_cuda")],
        snapshots={
            "h1": _gpu_snap([(0, 20.0), (1, 20.0), (2, 20.0)], vram_available=60.0)
        },
    )
    reason = _shortfall_reason(intent, observed)
    assert reason == "needs 1 GPU with 50 GB each; best eligible host offers 0"


def test_shortfall_counts_devices_closed_by_claims():
    """The message honours the control-side claim ledger (observed's
    pre-fetched gpu_claims): a claim dropping a device's free memory below
    the footprint removes it from the offered count."""
    intent = _make_intent(
        replicas=2,
        resources=ResourceRequirements(vram_gb=15.0, gpu_count=3),
    )
    observed = _make_observed(
        hosts=[_HostStub(id="h1", gpu_type="nvidia_cuda")],
        snapshots={
            "h1": _gpu_snap([(0, 20.0), (1, 20.0), (2, 20.0)], vram_available=60.0)
        },
    )
    # 10 GB already claimed on every device → 10 GB free each < 15 GB.
    observed["gpu_claims"] = {"h1": {0: 10.0, 1: 10.0, 2: 10.0}}
    reason = _shortfall_reason(intent, observed)
    assert reason == "needs 3 GPUs with 15 GB each; best eligible host offers 0"
