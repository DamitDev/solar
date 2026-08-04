"""Tests for host draining (S-043).

Covers the classification helpers, the preflight blockers, the drain status
view, the draining → drained sweep, and the drain routes.

Specification: training-platform-project/docs/specs/host-draining.md
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.models import DrainState, Host, HostStatus
from app.services import drain

MANAGEMENT_HEADERS = {"X-API-Key": "change-me-management"}


# ── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
def host() -> Host:
    return Host(
        id="h-1",
        name="Node A",
        url="http://node-a:8000",
        api_key="k",
        status=HostStatus.ONLINE,
        roles=["inference"],
    )


@pytest.fixture
def draining_host(host: Host) -> Host:
    host.drain_state = DrainState.DRAINING
    return host


def _managed(inst_id: str, alias: str = "iris:v1", status: str = "running") -> dict:
    return {
        "instance_id": inst_id,
        "status": status,
        "config": {
            "alias": alias,
            "managed_by": "intent",
            "intent_id": "intent-1",
        },
    }


def _manual(inst_id: str, alias: str = "scratch:v1", status: str = "running") -> dict:
    return {
        "instance_id": inst_id,
        "status": status,
        "config": {"alias": alias},
    }


def _patch_instances(instances: list[dict]):
    return patch(
        "app.redis_state.host_store.get_host_instances",
        new=AsyncMock(return_value=instances),
    )


def _patch_no_jobs():
    return patch(
        "app.services.migration.active_job_ids_on_host",
        new=AsyncMock(return_value=[]),
    )


def _patch_no_stalls():
    return patch("app.services.drain.get_stalls", new=AsyncMock(return_value={}))


# ── Ownership and activity helpers ──────────────────────────────


def test_is_managed_requires_both_markers():
    """managed_by alone does not make an instance intent-owned (§5.4)."""
    assert drain.is_managed(_managed("i-1")) is True
    assert drain.is_managed(_manual("i-2")) is False
    assert drain.is_managed({"instance_id": "i-3", "managed_by": "intent"}) is False
    assert drain.is_managed({"instance_id": "i-4", "intent_id": "intent-1"}) is False


def test_is_managed_reads_flat_instances():
    """Cache entries without a nested config are classified the same."""
    flat = {"instance_id": "i-1", "managed_by": "intent", "intent_id": "intent-1"}
    assert drain.is_managed(flat) is True
    assert drain.owning_intent_id(flat) == "intent-1"


@pytest.mark.parametrize(
    ("status", "active"),
    [
        ("running", True),
        ("starting", True),
        ("stopped", False),
        ("failed", False),
        ("exited", False),
    ],
)
def test_is_active(status, active):
    assert drain.is_active(_manual("i-1", status=status)) is active


# ── Preflight blockers ──────────────────────────────────────────


@pytest.mark.anyio
async def test_no_blockers_for_managed_instances_only(host):
    """Intent-managed replicas never block a drain — they get evacuated."""
    with _patch_instances([_managed("i-1"), _managed("i-2")]), _patch_no_jobs():
        assert await drain.collect_blockers(host) == []


@pytest.mark.anyio
async def test_running_manual_instance_blocks(host):
    with _patch_instances([_managed("i-1"), _manual("i-2")]), _patch_no_jobs():
        blockers = await drain.collect_blockers(host)

    assert [(b.kind, b.id) for b in blockers] == [("manual_instance", "i-2")]


@pytest.mark.anyio
async def test_stopped_manual_instance_does_not_block(host):
    """The preflight asks for manual instances to be stopped, not deleted."""
    with _patch_instances([_manual("i-2", status="stopped")]), _patch_no_jobs():
        assert await drain.collect_blockers(host) == []


@pytest.mark.anyio
async def test_active_job_blocks(host):
    """Migration refuses a source host with active steps, so drain must too."""
    with (
        _patch_instances([_managed("i-1")]),
        patch(
            "app.services.migration.active_job_ids_on_host",
            new=AsyncMock(return_value=["job-7"]),
        ),
    ):
        blockers = await drain.collect_blockers(host)

    assert [(b.kind, b.id) for b in blockers] == [("active_job", "job-7")]


# ── Drain status ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_drain_status_counts_remaining_work(draining_host):
    instances = [
        _managed("i-1"),
        _managed("i-2", alias="lily:v2"),
        _manual("i-3"),
        _manual("i-4", status="stopped"),
    ]
    with _patch_instances(instances), _patch_no_jobs(), _patch_no_stalls():
        status = await drain.build_drain_status(draining_host)

    assert status.drain_state == DrainState.DRAINING
    assert status.managed_remaining == 2
    assert status.manual_running == 1
    assert [r.instance_id for r in status.replicas] == ["i-1", "i-2"]
    assert all(r.intent_id == "intent-1" for r in status.replicas)
    assert status.stalled is False


@pytest.mark.anyio
async def test_drain_status_stalled_when_every_replica_is_blocked(draining_host):
    """Stalled means the drain cannot progress without operator action (§4.3)."""
    with (
        _patch_instances([_managed("i-1"), _managed("i-2")]),
        _patch_no_jobs(),
        patch(
            "app.services.drain.get_stalls",
            new=AsyncMock(return_value={"i-1": "no target", "i-2": "no target"}),
        ),
    ):
        status = await drain.build_drain_status(draining_host)

    assert status.stalled is True
    assert [r.blocked_reason for r in status.replicas] == ["no target", "no target"]


@pytest.mark.anyio
async def test_drain_status_not_stalled_while_one_replica_can_move(draining_host):
    with (
        _patch_instances([_managed("i-1"), _managed("i-2")]),
        _patch_no_jobs(),
        patch(
            "app.services.drain.get_stalls",
            new=AsyncMock(return_value={"i-1": "no target"}),
        ),
    ):
        status = await drain.build_drain_status(draining_host)

    assert status.stalled is False


@pytest.mark.anyio
async def test_drain_status_of_undrained_host_is_never_stalled(host):
    with (
        _patch_instances([_managed("i-1")]),
        _patch_no_jobs(),
        patch(
            "app.services.drain.get_stalls",
            new=AsyncMock(return_value={"i-1": "no target"}),
        ),
    ):
        status = await drain.build_drain_status(host)

    assert status.drain_state is None
    assert status.stalled is False


# ── Completion sweep ────────────────────────────────────────────


@pytest.mark.anyio
async def test_host_empty_ignores_stopped_manual_instances():
    with _patch_instances([_manual("i-1", status="stopped")]):
        assert await drain.is_host_empty("h-1") is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    "instances",
    [
        pytest.param([_managed("i-1", status="stopped")], id="stopped-managed"),
        pytest.param([_manual("i-1")], id="running-manual"),
    ],
)
async def test_host_not_empty(instances):
    """A managed instance keeps the host busy even when it is not running:
    the reconciler still owns it, so the evacuation is not finished."""
    with _patch_instances(instances):
        assert await drain.is_host_empty("h-1") is False


@pytest.mark.anyio
async def test_sweep_promotes_empty_draining_host(draining_host):
    drained = draining_host.model_copy(update={"drain_state": DrainState.DRAINED})
    set_state = AsyncMock(return_value=drained)

    with (
        patch(
            "app.database.hosts.host_db.list_draining_hosts",
            new=AsyncMock(return_value=[draining_host]),
        ),
        patch("app.database.hosts.host_db.set_drain_state", new=set_state),
        patch("app.services.drain.is_host_empty", new=AsyncMock(return_value=True)),
        patch("app.services.drain.broadcast_drain_state", new=AsyncMock()) as broadcast,
        patch("app.services.drain.redis_client") as mock_redis,
    ):
        mock_redis.return_value.set = AsyncMock(return_value=True)
        mock_redis.return_value.delete = AsyncMock()

        await drain.sweep_drained_hosts()

    set_state.assert_awaited_once_with(draining_host.id, DrainState.DRAINED)
    assert broadcast.await_args.args[0].drain_state == DrainState.DRAINED


@pytest.mark.anyio
async def test_sweep_leaves_non_empty_host_draining(draining_host):
    set_state = AsyncMock()

    with (
        patch(
            "app.database.hosts.host_db.list_draining_hosts",
            new=AsyncMock(return_value=[draining_host]),
        ),
        patch("app.database.hosts.host_db.set_drain_state", new=set_state),
        patch("app.services.drain.is_host_empty", new=AsyncMock(return_value=False)),
        patch("app.services.drain.redis_client") as mock_redis,
    ):
        mock_redis.return_value.set = AsyncMock(return_value=True)
        mock_redis.return_value.delete = AsyncMock()

        await drain.sweep_drained_hosts()

    set_state.assert_not_awaited()


@pytest.mark.anyio
async def test_sweep_skips_when_another_replica_holds_the_lock(draining_host):
    """Only the lock holder promotes, so the event is broadcast once."""
    set_state = AsyncMock()

    with (
        patch(
            "app.database.hosts.host_db.list_draining_hosts",
            new=AsyncMock(return_value=[draining_host]),
        ),
        patch("app.database.hosts.host_db.set_drain_state", new=set_state),
        patch("app.services.drain.is_host_empty", new=AsyncMock(return_value=True)),
        patch("app.services.drain.redis_client") as mock_redis,
    ):
        mock_redis.return_value.set = AsyncMock(return_value=None)  # NX failed
        mock_redis.return_value.delete = AsyncMock()

        await drain.sweep_drained_hosts()

    set_state.assert_not_awaited()


# ── Routes ──────────────────────────────────────────────────────


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)


@pytest.mark.anyio
async def test_drain_route_marks_host_draining(host):
    draining = host.model_copy(update={"drain_state": DrainState.DRAINING})

    with (
        patch(
            "app.routes.management.hosts.host_db.get_host",
            new=AsyncMock(return_value=host),
        ),
        patch(
            "app.routes.management.hosts.host_db.set_drain_state",
            new=AsyncMock(return_value=draining),
        ) as set_state,
        patch("app.services.drain.collect_blockers", new=AsyncMock(return_value=[])),
        patch("app.services.drain.broadcast_drain_state", new=AsyncMock()),
        _patch_instances([_managed("i-1")]),
        _patch_no_stalls(),
    ):
        response = _client().post(
            f"/api/hosts/{host.id}/drain", headers=MANAGEMENT_HEADERS
        )

    assert response.status_code == 202
    body = response.json()
    assert body["drain_state"] == "draining"
    assert body["managed_remaining"] == 1
    set_state.assert_awaited_once_with(host.id, DrainState.DRAINING)


@pytest.mark.anyio
async def test_drain_route_reports_blockers_as_409(host):
    from app.models import DrainBlocker

    blocker = DrainBlocker(
        kind="manual_instance", id="i-2", name="scratch:v1", detail="still running"
    )

    with (
        patch(
            "app.routes.management.hosts.host_db.get_host",
            new=AsyncMock(return_value=host),
        ),
        patch(
            "app.routes.management.hosts.host_db.set_drain_state", new=AsyncMock()
        ) as set_state,
        patch(
            "app.services.drain.collect_blockers",
            new=AsyncMock(return_value=[blocker]),
        ),
    ):
        response = _client().post(
            f"/api/hosts/{host.id}/drain", headers=MANAGEMENT_HEADERS
        )

    assert response.status_code == 409
    assert response.json()["detail"]["blockers"][0]["id"] == "i-2"
    set_state.assert_not_awaited()


@pytest.mark.anyio
async def test_drain_route_is_idempotent(draining_host):
    """Draining an already draining host reports status without re-marking."""
    with (
        patch(
            "app.routes.management.hosts.host_db.get_host",
            new=AsyncMock(return_value=draining_host),
        ),
        patch(
            "app.routes.management.hosts.host_db.set_drain_state", new=AsyncMock()
        ) as set_state,
        _patch_instances([]),
        _patch_no_jobs(),
        _patch_no_stalls(),
    ):
        response = _client().post(
            f"/api/hosts/{draining_host.id}/drain", headers=MANAGEMENT_HEADERS
        )

    assert response.status_code == 202
    assert response.json()["drain_state"] == "draining"
    set_state.assert_not_awaited()


@pytest.mark.anyio
async def test_resume_route_clears_drain_state(draining_host):
    resumed = draining_host.model_copy(update={"drain_state": None})

    with (
        patch(
            "app.routes.management.hosts.host_db.get_host",
            new=AsyncMock(return_value=draining_host),
        ),
        patch(
            "app.routes.management.hosts.host_db.set_drain_state",
            new=AsyncMock(return_value=resumed),
        ) as set_state,
        patch("app.services.drain.broadcast_drain_state", new=AsyncMock()),
        _patch_instances([]),
        _patch_no_jobs(),
        _patch_no_stalls(),
    ):
        response = _client().delete(
            f"/api/hosts/{draining_host.id}/drain", headers=MANAGEMENT_HEADERS
        )

    assert response.status_code == 200
    assert response.json()["drain_state"] is None
    set_state.assert_awaited_once_with(draining_host.id, None)


@pytest.mark.anyio
async def test_drain_status_route_404_for_unknown_host():
    with patch(
        "app.routes.management.hosts.host_db.get_host",
        new=AsyncMock(return_value=None),
    ):
        response = _client().get("/api/hosts/nope/drain", headers=MANAGEMENT_HEADERS)

    assert response.status_code == 404


@pytest.mark.anyio
async def test_manual_instance_creation_rejected_on_draining_host(draining_host):
    """New manual work bypasses placement, so the route has to refuse it."""
    with patch(
        "app.routes.management.hosts.host_db.get_host",
        new=AsyncMock(return_value=draining_host),
    ):
        response = _client().post(
            f"/api/hosts/{draining_host.id}/instances",
            json={"alias": "scratch:v1", "backend_type": "llamacpp"},
            headers=MANAGEMENT_HEADERS,
        )

    assert response.status_code == 409
    assert "draining" in response.json()["detail"]
