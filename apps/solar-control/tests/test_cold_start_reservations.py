"""Tests for reconciler cold-start reservations and per-action timeouts.

Covers:
- _action_timeout_s: cold-start-capable actions get the long bound, quick
  ops keep the short one.
- _reserve_cold_start: reserves the intent's VRAM on the selected host
  before the pull, is idempotent per (intent, host), skips intents without
  a VRAM estimate, and propagates the host's capacity 409 as a fail-fast.
- _release_finished_reservations: releases when the instance is running or
  gone, keeps holding while the instance is still starting.
"""

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
    _action_timeout_s,
)


def _make_intent(**overrides) -> IntentResponse:
    defaults = {
        "id": "intent-001",
        "alias": "test-model",
        "model_source": "repo://test:v1",
        "replicas": 1,
        "priority": "production",
        "strategy": "rolling",
        "backend": {"backend_type": "huggingface_classification", "max_length": 512},
        "placement": PlacementConstraints(),
        "resources": ResourceRequirements(vram_gb=16.0),
        "metadata": {},
        "status": IntentStatus(
            phase=IntentPhase.RECONCILING,
            reconcile=ReconcileState.IN_PROGRESS,
            desired_replicas=1,
        ),
    }
    defaults.update(overrides)
    return IntentResponse(**defaults)


def _make_action(action_type: str, host_id: str = "host-1") -> Action:
    return Action(
        type=action_type,
        intent_id="intent-001",
        alias="test-model",
        host_id=host_id,
        instance_id="inst-1",
        reason="test",
    )


def _reconciler() -> Reconciler:
    return Reconciler()


# ── Per-action-type timeout ─────────────────────────────────────


class TestActionTimeout:
    def test_cold_start_actions_get_long_bound(self):
        for action_type in (ActionType.CREATE, ActionType.EVACUATE, ActionType.MIGRATE):
            bound = _action_timeout_s(_make_action(action_type))
            assert bound > 600  # minutes, not seconds

    def test_quick_actions_keep_short_bound(self):
        for action_type in (
            ActionType.STOP,
            ActionType.REPLACE,
            ActionType.RECREATE,
            ActionType.DISOWN,
            ActionType.NOOP,
        ):
            assert _action_timeout_s(_make_action(action_type)) == 60


# ── _reserve_cold_start ─────────────────────────────────────────


class TestReserveColdStart:
    @pytest.mark.anyio
    async def test_reserves_vram_on_selected_host(self):
        intent = _make_intent()
        with (
            patch(
                "app.services.reservation.reserve_host_capacity",
                new=AsyncMock(return_value="res-1"),
            ) as reserve,
            patch(
                "app.services.reservation.get_reconcile_reservations",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.services.reservation.store_reconcile_reservation",
                new=AsyncMock(),
            ) as store,
            patch(
                "app.database.hosts.host_db.get_host",
                new=AsyncMock(return_value=SimpleNamespace(id="host-1", name="h1")),
            ),
        ):
            await _reconciler()._reserve_cold_start(intent, "host-1")

        reserve.assert_awaited_once()
        args = reserve.await_args
        assert args.kwargs["vram_gb"] == 16.0
        assert args.kwargs["job_id"] == "intent:intent-001"
        assert args.kwargs["ttl_seconds"] is not None
        store.assert_awaited_once_with("intent-001", "host-1", "res-1", 16.0, 0.0)

    @pytest.mark.anyio
    async def test_skips_intent_without_vram_estimate(self):
        intent = _make_intent(resources=ResourceRequirements())
        with (
            patch(
                "app.services.reservation.reserve_host_capacity",
                new=AsyncMock(),
            ) as reserve,
            patch(
                "app.services.reservation.get_reconcile_reservations",
                new=AsyncMock(),
            ),
            patch(
                "app.services.reservation.store_reconcile_reservation",
                new=AsyncMock(),
            ),
            patch("app.database.hosts.host_db.get_host", new=AsyncMock()),
        ):
            await _reconciler()._reserve_cold_start(intent, "host-1")

        reserve.assert_not_awaited()

    @pytest.mark.anyio
    async def test_idempotent_per_intent_host(self):
        intent = _make_intent()
        with (
            patch(
                "app.services.reservation.reserve_host_capacity",
                new=AsyncMock(),
            ) as reserve,
            patch(
                "app.services.reservation.get_reconcile_reservations",
                new=AsyncMock(
                    return_value={"host-1": {"host_reservation_id": "res-1"}}
                ),
            ),
            patch(
                "app.services.reservation.store_reconcile_reservation",
                new=AsyncMock(),
            ),
            patch("app.database.hosts.host_db.get_host", new=AsyncMock()),
        ):
            await _reconciler()._reserve_cold_start(intent, "host-1")

        reserve.assert_not_awaited()

    @pytest.mark.anyio
    async def test_capacity_409_aborts(self):
        """A host that cannot fit the estimate raises → the action fails fast,
        before any model download starts."""
        intent = _make_intent()
        with (
            patch(
                "app.services.reservation.reserve_host_capacity",
                new=AsyncMock(
                    side_effect=HTTPException(
                        status_code=409, detail="Capacity exceeded for vram"
                    )
                ),
            ),
            patch(
                "app.services.reservation.get_reconcile_reservations",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "app.services.reservation.store_reconcile_reservation",
                new=AsyncMock(),
            ),
            patch(
                "app.database.hosts.host_db.get_host",
                new=AsyncMock(return_value=SimpleNamespace(id="host-1", name="h1")),
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await _reconciler()._reserve_cold_start(intent, "host-1")
        assert exc.value.status_code == 409


# ── _release_finished_reservations ──────────────────────────────


def _instance(instance_id: str, status: str, intent_id: str = "intent-001") -> dict:
    return {"instance_id": instance_id, "intent_id": intent_id, "status": status}


class TestReleaseFinishedReservations:
    @pytest.mark.anyio
    async def test_releases_when_instance_running(self):
        with (
            patch(
                "app.services.reservation.get_reconcile_reservations",
                new=AsyncMock(
                    return_value={"host-1": {"host_reservation_id": "res-1"}}
                ),
            ),
            patch(
                "app.redis_state.host_store.get_host_instances",
                new=AsyncMock(return_value=[_instance("inst-1", "running")]),
            ),
            patch(
                "app.database.hosts.host_db.get_host",
                new=AsyncMock(return_value=SimpleNamespace(id="host-1", name="h1")),
            ),
            patch(
                "app.services.reservation.release_host_capacity",
                new=AsyncMock(),
            ) as release,
            patch(
                "app.services.reservation.remove_reconcile_reservation",
                new=AsyncMock(),
            ) as remove,
        ):
            await _reconciler()._release_finished_reservations(_make_intent())

        release.assert_awaited_once()
        remove.assert_awaited_once_with("intent-001", "host-1")

    @pytest.mark.anyio
    async def test_keeps_holding_while_instance_starting(self):
        with (
            patch(
                "app.services.reservation.get_reconcile_reservations",
                new=AsyncMock(
                    return_value={"host-1": {"host_reservation_id": "res-1"}}
                ),
            ),
            patch(
                "app.redis_state.host_store.get_host_instances",
                new=AsyncMock(return_value=[_instance("inst-1", "starting")]),
            ),
            patch("app.database.hosts.host_db.get_host", new=AsyncMock()),
            patch(
                "app.services.reservation.release_host_capacity",
                new=AsyncMock(),
            ) as release,
            patch(
                "app.services.reservation.remove_reconcile_reservation",
                new=AsyncMock(),
            ) as remove,
        ):
            await _reconciler()._release_finished_reservations(_make_intent())

        release.assert_not_awaited()
        remove.assert_not_awaited()

    @pytest.mark.anyio
    async def test_releases_when_instance_gone(self):
        """A definitive failure deleted the instance → nothing to protect."""
        with (
            patch(
                "app.services.reservation.get_reconcile_reservations",
                new=AsyncMock(
                    return_value={"host-1": {"host_reservation_id": "res-1"}}
                ),
            ),
            patch(
                "app.redis_state.host_store.get_host_instances",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.database.hosts.host_db.get_host",
                new=AsyncMock(return_value=SimpleNamespace(id="host-1", name="h1")),
            ),
            patch(
                "app.services.reservation.release_host_capacity",
                new=AsyncMock(),
            ) as release,
            patch(
                "app.services.reservation.remove_reconcile_reservation",
                new=AsyncMock(),
            ) as remove,
        ):
            await _reconciler()._release_finished_reservations(_make_intent())

        release.assert_awaited_once()
        remove.assert_awaited_once_with("intent-001", "host-1")

    @pytest.mark.anyio
    async def test_release_failure_still_clears_tracking(self):
        """A failed release (host unreachable) logs a warning but clears the
        tracking entry — the host-side TTL reaper is the backstop for the
        reservation itself."""
        with (
            patch(
                "app.services.reservation.get_reconcile_reservations",
                new=AsyncMock(
                    return_value={"host-1": {"host_reservation_id": "res-1"}}
                ),
            ),
            patch(
                "app.redis_state.host_store.get_host_instances",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "app.database.hosts.host_db.get_host",
                new=AsyncMock(return_value=SimpleNamespace(id="host-1", name="h1")),
            ),
            patch(
                "app.services.reservation.release_host_capacity",
                new=AsyncMock(
                    side_effect=HTTPException(status_code=502, detail="unreachable")
                ),
            ),
            patch(
                "app.services.reservation.remove_reconcile_reservation",
                new=AsyncMock(),
            ) as remove,
        ):
            await _reconciler()._release_finished_reservations(_make_intent())

        remove.assert_awaited_once_with("intent-001", "host-1")
