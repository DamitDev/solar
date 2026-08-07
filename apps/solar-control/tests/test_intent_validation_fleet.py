"""C3 fleet-aware intent validation.

Hard errors for durable, static facts (unknown host ids, device vs
allow-list accelerators); advisory warnings for dynamic fleet state —
and warnings must never block an edit.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.models import Host, HostStatus


def _host(host_id: str, gpu_type: str = "cpu", drain_state=None) -> Host:
    return Host(
        id=host_id,
        name=host_id,
        url=f"http://{host_id}:8000",
        api_key="k",
        status=HostStatus.ONLINE,
        roles=["inference"],
        gpu_type=gpu_type,
        drain_state=drain_state,
    )


class _Snap:
    def __init__(self, host_id, vram=64.0, ram=128.0, reachable=True):
        self.host_id = host_id
        self.vram_available_gb = vram
        self.ram_available_gb = ram
        self.reachable = reachable
        self.disk_available_gb = None
        self.running_instance_count = 0


def _payload(**overrides) -> dict:
    data = {
        "alias": "t",
        "model_source": "repo://test:v1",
        "replicas": 1,
        "priority": "production",
        "strategy": "rolling",
        "backend": {"backend_type": "huggingface_classification", "device": "cpu"},
        "placement": {},
        "resources": {},
    }
    data.update(overrides)
    return data


async def _validate(payload, hosts, snapshots=None, connected=None):
    from app.services.intent_validation import validate_intent_fleet

    snapshots = snapshots or {h.id: _Snap(h.id) for h in hosts}
    connected = connected if connected is not None else {h.id for h in hosts}
    with (
        patch(
            "app.database.hosts.host_db.get_all_hosts",
            new=AsyncMock(return_value=hosts),
        ),
        patch(
            "app.routes.management.resources._fetch_host_resource_snapshot",
            new=AsyncMock(side_effect=lambda h: snapshots[h.id]),
        ),
        patch(
            "app.redis_state.host_store.get_connected_host_ids",
            new=AsyncMock(return_value=list(connected)),
        ),
    ):
        return await validate_intent_fleet(payload)


class TestReconcilerDefensiveValidation:
    """C5: a reconcile tick must not pay for the advisory half of validation."""

    @pytest.mark.anyio
    async def test_tick_reuses_observed_hosts_and_reads_no_snapshots(self):
        """The per-tick check runs off _observe's roster — no snapshot reads.

        _fetch_host_resource_snapshot falls through to a 5 s HTTP call per host
        whenever a host is stale or disconnected, which is exactly the cost C5
        set out to remove from the reconcile path.
        """
        from test_reconciliation import _HostStub, _make_intent, _make_observed

        from app.services.reconciliation import Reconciler

        reconciler = Reconciler()
        intent = _make_intent(replicas=0)
        observed = _make_observed(managed=[], hosts=[_HostStub(id="h1")])

        with (
            patch.object(reconciler, "_observe", new=AsyncMock(return_value=observed)),
            patch.object(reconciler, "_update_status", new=AsyncMock()),
            patch(
                "app.routes.management.resources._fetch_host_resource_snapshot",
                new=AsyncMock(side_effect=AssertionError("snapshot read on tick")),
            ),
            patch(
                "app.database.hosts.host_db.get_all_hosts",
                new=AsyncMock(side_effect=AssertionError("host refetch on tick")),
            ),
        ):
            await reconciler._reconcile_one(intent)

    @pytest.mark.anyio
    async def test_violations_are_logged_once_per_spec_version(self):
        """An unfixable legacy spec must not log once per tick forever."""
        from test_reconciliation import _HostStub, _make_intent, _make_observed

        from app.services.reconciliation import Reconciler

        reconciler = Reconciler()
        # host_allow names a host that is not in the roster: a hard violation.
        intent = _make_intent(replicas=0)
        intent.placement.host_allow = ["ghost"]
        observed = _make_observed(managed=[], hosts=[_HostStub(id="h1")])

        with (
            patch.object(reconciler, "_observe", new=AsyncMock(return_value=observed)),
            patch.object(reconciler, "_update_status", new=AsyncMock()),
            patch("app.services.reconciliation.logger") as mock_logger,
        ):
            for _ in range(4):
                await reconciler._reconcile_one(intent)

        violations = [
            c
            for c in mock_logger.error.call_args_list
            if "violates fleet validation" in str(c)
        ]
        assert len(violations) == 1


class TestHardErrors:
    @pytest.mark.anyio
    async def test_unknown_host_allow_id_is_hard(self):
        hosts = [_host("h1"), _host("h2")]
        payload = _payload(placement={"host_allow": ["h1", "ghost"]})
        hard, _ = await _validate(payload, hosts)
        assert any(
            e["field"] == "placement.host_allow" and "ghost" in e["message"]
            for e in hard
        )

    @pytest.mark.anyio
    async def test_unknown_host_deny_id_is_hard(self):
        hosts = [_host("h1")]
        payload = _payload(placement={"host_deny": ["ghost"]})
        hard, _ = await _validate(payload, hosts)
        assert any(e["field"] == "placement.host_deny" for e in hard)

    @pytest.mark.anyio
    async def test_device_requiring_missing_allowlist_accelerator_is_hard(self):
        """The reported symptom: device mps + an NVIDIA-only allow list."""
        hosts = [_host("h1", gpu_type="nvidia_cuda")]
        payload = _payload(
            backend={
                "backend_type": "huggingface_classification",
                "device": "mps",
            },
            placement={"host_allow": ["h1"]},
        )
        hard, _ = await _validate(payload, hosts)
        assert any(
            e["field"] == "backend.device" and "apple_mps" in e["message"] for e in hard
        )

    @pytest.mark.anyio
    async def test_device_matching_allowlist_accelerator_passes(self):
        hosts = [_host("h1", gpu_type="apple_mps")]
        payload = _payload(
            backend={
                "backend_type": "huggingface_classification",
                "device": "mps",
            },
            placement={"host_allow": ["h1"]},
        )
        hard, _ = await _validate(payload, hosts)
        assert hard == []


class TestWarnings:
    @pytest.mark.anyio
    async def test_replicas_above_eligible_hosts_warns(self):
        hosts = [_host("h1"), _host("h2")]
        payload = _payload(replicas=5)
        hard, warnings = await _validate(payload, hosts)
        assert hard == []
        assert any(w["field"] == "replicas" for w in warnings)

    @pytest.mark.anyio
    async def test_vram_above_fleet_capacity_warns(self):
        hosts = [_host("h1")]
        payload = _payload(resources={"vram_gb": 128.0})
        hard, warnings = await _validate(payload, hosts)
        assert hard == []
        assert any(w["field"] == "resources.vram_gb" for w in warnings)

    @pytest.mark.anyio
    async def test_all_eligible_hosts_draining_warns(self):
        hosts = [
            _host("h1", drain_state="draining"),
            _host("h2", drain_state="draining"),
        ]
        payload = _payload(replicas=1)
        hard, warnings = await _validate(payload, hosts)
        assert hard == []
        assert any("draining" in w["message"] for w in warnings)

    @pytest.mark.anyio
    async def test_valid_gpu_type_not_reported_warns(self):
        hosts = [_host("h1", gpu_type="cpu")]
        payload = _payload(placement={"gpu_type": "apple_mps"})
        hard, warnings = await _validate(payload, hosts)
        assert hard == []
        assert any(w["field"] == "placement.gpu_type" for w in warnings)

    @pytest.mark.anyio
    async def test_a_stored_gpu_type_alias_is_placeable_here_and_in_the_reconciler(
        self,
    ):
        """§3.5: eligibility reuses the real filter chain so validation and
        placement cannot disagree. Normalization ran only on write, so a row
        stored with an alias made exactly those two disagree — this asserts the
        agreement, not just that the validator normalizes."""
        from app.models.intent import PlacementConstraints
        from app.services.reconciliation import _requested_gpu_type

        hosts = [_host("h1", gpu_type="apple_mps")]
        payload = _payload(placement={"gpu_type": "mps"})

        hard, warnings = await _validate(payload, hosts)
        assert hard == []
        assert not any(w["field"] == "placement.gpu_type" for w in warnings)

        # The reconciler reads the same stored value off the model, not the
        # payload dict, so it needs its own normalization to reach the token.
        stored = PlacementConstraints(**payload["placement"])
        assert _requested_gpu_type(stored) == "apple_mps"

    @pytest.mark.anyio
    async def test_warnings_never_become_errors(self):
        """Dynamic fleet state must not block an edit."""
        hosts = [
            _host("h1", gpu_type="cpu", drain_state="draining"),
            _host("h2", gpu_type="cpu", drain_state="draining"),
        ]
        payload = _payload(
            replicas=9,
            resources={"vram_gb": 999.0},
            placement={"gpu_type": "cpu"},
        )
        hard, warnings = await _validate(payload, hosts)
        assert hard == []
        # replicas-above-eligible + vram-above-capacity + all-draining
        assert len(warnings) >= 3
        fields = {w["field"] for w in warnings}
        assert {"replicas", "resources.vram_gb"} <= fields
