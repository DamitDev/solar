"""C1 churn circuit breaker: drift-driven REPLACE rounds are bounded, and an
unsettled spec degrades into one clear BackendDriftUnsettled error instead
of a stop/recreate loop that never converges."""

from unittest.mock import AsyncMock, patch

import pytest

from app.models.intent import (
    IntentPhase,
    IntentResponse,
    IntentStatus,
    ReconcileState,
)
from app.services.reconciliation import ActionType, Reconciler

from test_reconciliation import (
    _make_intent,
    _make_managed_instance,
    _make_observed,
)


def _drifted_intent(
    attempts: int = 0, phase: IntentPhase = IntentPhase.RECONCILING
) -> IntentResponse:
    """An intent whose spec is pending and whose replica carries a drifted
    chat_template_kwargs full config (spec dict vs compact string)."""
    return _make_intent(
        backend={
            "backend_type": "llamacpp",
            "chat_template_kwargs": {"enable_thinking": True},
        },
        status=IntentStatus(
            phase=phase,
            reconcile=ReconcileState.IN_PROGRESS,
            desired_replicas=1,
            spec_changed_at="2026-08-06T00:00:00+00:00",
            drift_replace_attempts=attempts,
        ),
    )


def _drifted_observed() -> dict:
    inst = _make_managed_instance("inst-1", backend_type="llamacpp")
    # _observe attaches the full config at the top level of the instance
    # dict while a spec change is pending; the flat cache entry stays in
    # ``config`` and carries almost none of the backend fields. The value is
    # genuinely different (enable_thinking true vs false), so drift must
    # fire even with the canonicalization-aware comparison.
    inst["_full_config"] = {
        "backend_type": "llamacpp",
        "chat_template_kwargs": '{"enable_thinking":false}',
    }
    return _make_observed(managed=[inst])


class TestDiffCircuitBreaker:
    def test_plans_replace_below_max_attempts(self):
        reconciler = Reconciler()
        intent = _drifted_intent(attempts=1)
        observed = _drifted_observed()
        actions = reconciler._diff(intent, observed)
        drift_replaces = [
            a
            for a in actions
            if a.type == ActionType.REPLACE and a.reason == "backend config drift"
        ]
        assert len(drift_replaces) == 1
        assert "_drift_unsettled" not in observed

    def test_stops_planning_at_max_attempts(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.max_drift_replace_attempts", 3)
        reconciler = Reconciler()
        intent = _drifted_intent(attempts=3)
        observed = _drifted_observed()
        actions = reconciler._diff(intent, observed)
        drift_replaces = [
            a
            for a in actions
            if a.type == ActionType.REPLACE and a.reason == "backend config drift"
        ]
        assert drift_replaces == []
        # The breaker marker names the mismatching keys.
        assert observed["_drift_unsettled"] == ["chat_template_kwargs"]

    def test_spec_settled_requires_no_unsettled_marker(self):
        from app.services.reconciliation import _spec_settled

        intent = _drifted_intent(attempts=3)
        observed = _drifted_observed()
        # Without a marker the observed replica matches -> settled.
        assert _spec_settled(observed, []) is True
        # With the breaker tripped the edit must stay pending.
        observed["_drift_unsettled"] = ["chat_template_kwargs"]
        assert _spec_settled(observed, []) is False


class TestUpdateStatusCircuitBreaker:
    @pytest.mark.anyio
    async def test_increments_attempts_on_drift_replace(self):
        reconciler = Reconciler()
        intent = _drifted_intent(attempts=2)
        with patch("app.database.intents.intent_db") as mock_db:
            mock_db.update_status = AsyncMock()
            mock_db.get_intent = AsyncMock(return_value=intent)
            await reconciler._update_status(
                intent, _drifted_observed(), drift_replace=True
            )
        status_json = mock_db.update_status.call_args.kwargs["status_json"]
        assert status_json["drift_replace_attempts"] == 3

    @pytest.mark.anyio
    async def test_resets_attempts_when_spec_settles(self):
        reconciler = Reconciler()
        intent = _drifted_intent(attempts=2)
        with patch("app.database.intents.intent_db") as mock_db:
            mock_db.update_status = AsyncMock()
            mock_db.get_intent = AsyncMock(return_value=intent)
            await reconciler._update_status(
                intent, _drifted_observed(), spec_settled=True
            )
        status_json = mock_db.update_status.call_args.kwargs["status_json"]
        assert status_json["drift_replace_attempts"] == 0

    @pytest.mark.anyio
    async def test_tripped_breaker_records_backend_drift_unsettled(self):
        """The loop ends in one clear, actionable error."""
        reconciler = Reconciler()
        intent = _drifted_intent(attempts=3, phase=IntentPhase.READY)
        observed = _drifted_observed()
        observed["_drift_unsettled"] = ["chat_template_kwargs"]
        with patch("app.database.intents.intent_db") as mock_db:
            mock_db.update_status = AsyncMock()
            mock_db.get_intent = AsyncMock(return_value=intent)
            await reconciler._update_status(intent, observed)
        status_json = mock_db.update_status.call_args.kwargs["status_json"]
        # Not converged: the replicas provably do not match the spec.
        assert mock_db.update_status.call_args.kwargs["phase"] == "degraded"
        assert status_json["spec_changed_at"] is not None
        assert status_json["drift_replace_attempts"] == 3
        assert status_json["last_error"]["code"] == "BackendDriftUnsettled"
        assert "chat_template_kwargs" in status_json["last_error"]["message"]
        reasons = {
            c["reason"] for c in status_json["conditions"] if c["type"] == "Degraded"
        }
        assert "DriftUnsettled" in reasons
