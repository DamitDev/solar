"""C1 churn circuit breaker: drift-driven REPLACE rounds are bounded, and an
unsettled spec degrades into one clear BackendDriftUnsettled error instead
of a stop/recreate loop that never converges."""

from unittest.mock import AsyncMock, patch

import pytest
from test_reconciliation import (
    _make_intent,
    _make_managed_instance,
    _make_observed,
)

from app.models.intent import (
    IntentPhase,
    IntentResponse,
    IntentStatus,
    ReconcileState,
)
from app.services.reconciliation import ActionType, Reconciler


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
        # Persisted so a tick that never reaches _diff still knows.
        assert status_json["drift_unsettled_keys"] == ["chat_template_kwargs"]


class TestBreakerDoesNotFlap:
    """Only _diff can detect the tripped breaker, so the state has to live on
    the status: a tick routed through the settle window or a rollout strategy
    never populates ``observed`` and would otherwise flip back to Ready."""

    def _tripped_intent(self) -> IntentResponse:
        intent = _drifted_intent(attempts=3, phase=IntentPhase.DEGRADED)
        intent.status.drift_unsettled_keys = ["chat_template_kwargs"]
        intent.replicas = 1
        return intent

    def _ready_observed(self) -> dict:
        """One running replica registered in the gateway: fully converged."""
        return _make_observed(
            managed=[_make_managed_instance("inst-1")],
            gateway_aliases={"test-model"},
        )

    @pytest.mark.anyio
    async def test_phase_holds_when_observed_carries_no_marker(self):
        reconciler = Reconciler()
        intent = self._tripped_intent()
        # A ready replica and no marker: the naive version reports Ready.
        observed = self._ready_observed()
        with patch("app.database.intents.intent_db") as mock_db:
            mock_db.update_status = AsyncMock()
            mock_db.get_intent = AsyncMock(return_value=intent)
            await reconciler._update_status(intent, observed)
        kwargs = mock_db.update_status.call_args.kwargs
        assert kwargs["phase"] == "degraded"
        assert kwargs["status_json"]["drift_unsettled_keys"] == ["chat_template_kwargs"]
        assert kwargs["status_json"]["last_error"]["code"] == "BackendDriftUnsettled"

    @pytest.mark.anyio
    async def test_settling_the_spec_clears_the_persisted_keys(self):
        reconciler = Reconciler()
        intent = self._tripped_intent()
        observed = self._ready_observed()
        with patch("app.database.intents.intent_db") as mock_db:
            mock_db.update_status = AsyncMock()
            mock_db.get_intent = AsyncMock(return_value=intent)
            await reconciler._update_status(intent, observed, spec_settled=True)
        status_json = mock_db.update_status.call_args.kwargs["status_json"]
        assert status_json["drift_unsettled_keys"] == []
        assert status_json["drift_replace_attempts"] == 0
        assert status_json["last_error"] is None
        assert mock_db.update_status.call_args.kwargs["phase"] == "ready"

    @pytest.mark.anyio
    async def test_a_real_action_error_still_wins_over_the_breaker(self):
        reconciler = Reconciler()
        intent = self._tripped_intent()
        observed = self._ready_observed()
        with patch("app.database.intents.intent_db") as mock_db:
            mock_db.update_status = AsyncMock()
            mock_db.get_intent = AsyncMock(return_value=intent)
            await reconciler._update_status(
                intent,
                observed,
                last_error={"code": "HostUnreachable", "message": "refused"},
            )
        status_json = mock_db.update_status.call_args.kwargs["status_json"]
        assert status_json["last_error"]["code"] == "HostUnreachable"


class TestDriftReplaceCounting:
    """The counter tracks attempts, so only the executed action counts."""

    def test_predicate_matches_only_the_drift_replace(self):
        from app.services.reconciliation import Action, _is_drift_replace

        def _action(type_, reason):
            return Action(
                type=type_, intent_id="i", alias="a", reason=reason, priority=20
            )

        assert _is_drift_replace(_action(ActionType.REPLACE, "backend config drift"))
        assert not _is_drift_replace(
            _action(ActionType.REPLACE, "model_source drift: a → b")
        )
        assert not _is_drift_replace(_action(ActionType.STOP, "backend config drift"))

    @pytest.mark.anyio
    async def test_a_replace_that_loses_the_sort_does_not_count(self):
        """A STOP outranks the REPLACE, so no replacement was attempted; the
        counter must not move or the breaker trips without ever having tried.

        ``initiate_strategy`` returns None here — a REPLACE normally hands off
        to the rollout state machine, and this is the path taken when it
        declines.
        """
        reconciler = Reconciler()
        intent = _drifted_intent(attempts=1)
        intent.replicas = 1
        observed = _drifted_observed()
        # Two replicas for one desired: _diff plans the drift REPLACE plus a
        # higher-priority STOP for the surplus.
        surplus = _make_managed_instance("inst-2", backend_type="llamacpp")
        surplus["_full_config"] = {"backend_type": "llamacpp"}
        observed["managed_instances"].append(surplus)
        observed["alias_instances"] = list(observed["managed_instances"])

        with (
            patch("app.database.intents.intent_db") as mock_db,
            patch("app.services.strategies.initiate_strategy", return_value=None),
            patch.object(reconciler, "_observe", new=AsyncMock(return_value=observed)),
            patch.object(reconciler, "_act", new=AsyncMock(return_value=True)) as act,
        ):
            mock_db.update_status = AsyncMock()
            mock_db.get_intent = AsyncMock(return_value=intent)
            await reconciler._reconcile_one(intent)

        executed = act.call_args.args[1]
        assert executed.type == ActionType.STOP
        status_json = mock_db.update_status.call_args.kwargs["status_json"]
        assert status_json["drift_replace_attempts"] == 1

    @pytest.mark.anyio
    async def test_initiating_a_strategy_does_not_count_an_attempt(self):
        """Handing the REPLACE to the rollout state machine is not an attempt;
        the strategy's own replacements are what the host acts on."""
        reconciler = Reconciler()
        intent = _drifted_intent(attempts=1)
        intent.replicas = 1
        observed = _drifted_observed()

        with (
            patch("app.database.intents.intent_db") as mock_db,
            patch.object(reconciler, "_observe", new=AsyncMock(return_value=observed)),
            patch.object(reconciler, "_act", new=AsyncMock()) as act,
        ):
            mock_db.update_status = AsyncMock()
            mock_db.get_intent = AsyncMock(return_value=intent)
            await reconciler._reconcile_one(intent)

        act.assert_not_awaited()
        status_json = mock_db.update_status.call_args.kwargs["status_json"]
        assert status_json["strategy_progress"] is not None
        assert status_json["drift_replace_attempts"] == 1

    @pytest.mark.anyio
    async def test_an_executed_drift_replace_counts(self):
        reconciler = Reconciler()
        intent = _drifted_intent(attempts=1)
        intent.replicas = 1
        observed = _drifted_observed()

        with (
            patch("app.database.intents.intent_db") as mock_db,
            patch("app.services.strategies.initiate_strategy", return_value=None),
            patch.object(reconciler, "_observe", new=AsyncMock(return_value=observed)),
            patch.object(reconciler, "_act", new=AsyncMock(return_value=True)) as act,
        ):
            mock_db.update_status = AsyncMock()
            mock_db.get_intent = AsyncMock(return_value=intent)
            await reconciler._reconcile_one(intent)

        assert act.call_args.args[1].type == ActionType.REPLACE
        status_json = mock_db.update_status.call_args.kwargs["status_json"]
        assert status_json["drift_replace_attempts"] == 2

    @pytest.mark.anyio
    async def test_a_failed_drift_replace_does_not_count(self):
        """A REPLACE the host refused says nothing about the drift.

        The breaker measures "the host keeps reproducing this drift". A
        REPLACE that failed for an unrelated reason never produced a
        replacement to compare, so counting it would let a host outage
        report itself as BackendDriftUnsettled.
        """
        reconciler = Reconciler()
        intent = _drifted_intent(attempts=1)
        intent.replicas = 1
        observed = _drifted_observed()

        with (
            patch("app.database.intents.intent_db") as mock_db,
            patch("app.services.strategies.initiate_strategy", return_value=None),
            patch.object(reconciler, "_observe", new=AsyncMock(return_value=observed)),
            patch.object(
                reconciler,
                "_act",
                new=AsyncMock(side_effect=RuntimeError("host unreachable")),
            ) as act,
        ):
            mock_db.update_status = AsyncMock()
            mock_db.get_intent = AsyncMock(return_value=intent)
            await reconciler._reconcile_one(intent)

        assert act.call_args.args[1].type == ActionType.REPLACE
        status_json = mock_db.update_status.call_args.kwargs["status_json"]
        assert status_json["drift_replace_attempts"] == 1
        # The failure is still visible; it is just not the breaker's business.
        assert status_json["last_error"]["code"] == "RuntimeError"

    @pytest.mark.anyio
    async def test_repeated_replace_failures_never_trip_the_breaker(self, monkeypatch):
        """A host outage lasting many ticks must not be diagnosed as drift."""
        monkeypatch.setattr("app.config.settings.max_drift_replace_attempts", 3)
        reconciler = Reconciler()
        intent = _drifted_intent(attempts=0)
        intent.replicas = 1

        for _ in range(5):
            # Each iteration stands for a tick after the failure backoff has
            # elapsed; without this the reconciler skips the intent and the
            # loop would prove nothing.
            reconciler._backoff_clear(intent.id)
            with (
                patch("app.database.intents.intent_db") as mock_db,
                patch("app.services.strategies.initiate_strategy", return_value=None),
                patch.object(
                    reconciler,
                    "_observe",
                    new=AsyncMock(return_value=_drifted_observed()),
                ),
                patch.object(
                    reconciler,
                    "_act",
                    new=AsyncMock(side_effect=RuntimeError("host unreachable")),
                ),
            ):
                mock_db.update_status = AsyncMock()
                mock_db.get_intent = AsyncMock(return_value=intent)
                await reconciler._reconcile_one(intent)

            status_json = mock_db.update_status.call_args.kwargs["status_json"]
            intent.status.drift_replace_attempts = status_json["drift_replace_attempts"]

        assert intent.status.drift_replace_attempts == 0
        assert status_json["last_error"]["code"] == "RuntimeError"
