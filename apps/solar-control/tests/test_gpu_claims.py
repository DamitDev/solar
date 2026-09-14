"""S-058: the control-side cold-start GPU claim ledger.

The claim hash (``solar:reconcile:gpu_claims:{host_id}``) is the booking
authority for in-flight cold starts: placement must see it until the host's
own snapshot reflects the reservation, and it must clear on release.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.models import GpuInfo, HostReservationSummary, HostResourceSnapshot, HostStatus
from app.services.placement import find_gpu_assignment
from app.services.reservation import (
    gpu_claims_by_device,
    remove_reconcile_reservation,
    store_reconcile_reservation,
)

# The cold-start TTL: model_pull_timeout_s + host_start_timeout_s + 600.
_STALE_AFTER_S = 1800.0 + 900.0 + 600.0


class _FakeRedis:
    """Minimal async hash store recording hdel calls."""

    def __init__(self) -> None:
        self.stored: dict[str, dict[str, str]] = {}
        self.hdel_calls: list[tuple[str, tuple[str, ...]]] = []

    async def hset(self, key: str, field: str, value: str) -> None:
        self.stored.setdefault(key, {})[field] = value

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.stored.get(key, {}))

    async def hdel(self, key: str, *fields: str) -> int:
        self.hdel_calls.append((key, fields))
        if key in self.stored:
            for f in fields:
                self.stored[key].pop(f, None)
        return 0


def _snap(
    reservations: list[HostReservationSummary] | None = None,
) -> HostResourceSnapshot:
    return HostResourceSnapshot(
        host_id="host-1",
        host_name="h1",
        url="http://h1:8000",
        status=HostStatus.ONLINE,
        roles=["inference"],
        gpu_type="nvidia_cuda",
        reachable=True,
        vram_available_gb=96.0,
        ram_available_gb=512.0,
        gpus=[
            GpuInfo(
                index=0, name="RTX 4090", total_gb=48.0, used_gb=18.0, available_gb=30.0
            ),
            GpuInfo(
                index=1, name="RTX 4090", total_gb=48.0, used_gb=18.0, available_gb=30.0
            ),
        ],
        reservations=reservations or [],
    )


def _reservation_summary(res_id: str) -> HostReservationSummary:
    return HostReservationSummary(
        id=res_id,
        job_id="job-1",
        workload_type="training",
        status="pending",
        vram_gb=10.0,
        gpu_ids=[0],
    )


@pytest.mark.anyio
async def test_store_writes_both_hashes_and_claims_are_counted() -> None:
    fake = _FakeRedis()
    with patch("app.services.reservation.redis_client", return_value=fake):
        await store_reconcile_reservation(
            "intent-1", "host-1", "res-1", 10.0, 0.0, gpu_ids=[0]
        )
        await store_reconcile_reservation(
            "intent-2", "host-1", "res-2", 12.0, 0.0, gpu_ids=[0]
        )
        claims = await gpu_claims_by_device("host-1", _snap())

    assert claims == {0: 22.0}
    # The reverse index is keyed by host, the tracking hash by intent.
    assert set(fake.stored["solar:reconcile:gpu_claims:host-1"]) == {
        "intent-1",
        "intent-2",
    }
    assert set(fake.stored["solar:reconcile:reservations:intent-1"]) == {"host-1"}


@pytest.mark.anyio
async def test_claim_already_reflected_in_snapshot_is_skipped() -> None:
    """A claim whose host reservation the snapshot already lists would be
    double-counted (host-side headroom includes it) — skip it."""
    fake = _FakeRedis()
    with patch("app.services.reservation.redis_client", return_value=fake):
        await store_reconcile_reservation(
            "intent-1", "host-1", "res-1", 10.0, 0.0, gpu_ids=[0]
        )
        claims = await gpu_claims_by_device(
            "host-1", _snap(reservations=[_reservation_summary("res-1")])
        )

    assert claims == {}


@pytest.mark.anyio
async def test_stale_claim_is_ignored() -> None:
    """A claim past the cold-start TTL is leaked — never blocks a device."""
    fake = _FakeRedis()
    old = datetime.now(UTC) - timedelta(seconds=_STALE_AFTER_S + 100)
    claim = {
        "gpu_ids": [0],
        "vram_gb": 10.0,
        "host_reservation_id": "res-dead",
        "at": old.isoformat(),
    }
    fake.stored["solar:reconcile:gpu_claims:host-1"] = {"intent-1": json.dumps(claim)}
    with patch("app.services.reservation.redis_client", return_value=fake):
        claims = await gpu_claims_by_device("host-1", _snap())

    assert claims == {}


@pytest.mark.anyio
async def test_two_concurrent_intents_cannot_claim_the_same_device() -> None:
    """Spec §6: the claim ledger prevents double-booking a device."""
    fake = _FakeRedis()
    snap = _snap()
    with patch("app.services.reservation.redis_client", return_value=fake):
        await store_reconcile_reservation(
            "intent-1", "host-1", "res-a", 10.0, 0.0, gpu_ids=[0]
        )
        claims = await gpu_claims_by_device("host-1", snap)
        # Device 0 is claimed → placement must pick device 1.
        assert find_gpu_assignment(snap, 1, 10.0, claims=claims) == [1]

        # Release clears the claim → device 0 returns to the pool.
        await remove_reconcile_reservation("intent-1", "host-1")
        claims = await gpu_claims_by_device("host-1", snap)
        assert find_gpu_assignment(snap, 1, 10.0, claims=claims) == [0]


@pytest.mark.anyio
async def test_release_clears_both_hashes() -> None:
    fake = _FakeRedis()
    with patch("app.services.reservation.redis_client", return_value=fake):
        await store_reconcile_reservation(
            "intent-1", "host-1", "res-a", 10.0, 0.0, gpu_ids=[0, 1]
        )
        await remove_reconcile_reservation("intent-1", "host-1")

    assert fake.stored.get("solar:reconcile:gpu_claims:host-1", {}) == {}
    assert fake.stored.get("solar:reconcile:reservations:intent-1", {}) == {}
    keys = {k for k, _ in fake.hdel_calls}
    assert keys == {
        "solar:reconcile:reservations:intent-1",
        "solar:reconcile:gpu_claims:host-1",
    }
