"""C4 action timeout semantics.

Root-cause-A regression: cold-start actions (CREATE/EVACUATE/MIGRATE) are
bounded by ``_action_timeout_s(action)`` — not the raw 60 s ``_ACTION_TIMEOUT_S``
the normal reconcile flow used to apply to everything — and the wait is
progress-aware: while the host reports fresh pull progress, the wait
continues, and giving up mid-progress marks the recorded error recoverable.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from test_reconciliation import (
    _HostStub,
    _make_intent,
    _make_observed,
    _SnapshotStub,
)

from app.services.reconciliation import (
    Action,
    ActionType,
    Reconciler,
    _await_action_with_progress,
)


def _action(type_: str, **overrides) -> Action:
    return Action(
        type=type_,
        intent_id="intent-001",
        alias="test-model",
        host_id="h1",
        **overrides,
    )


async def _sleep_coro(seconds: float):
    await asyncio.sleep(seconds)
    return {"done": True}


class TestActionTimeoutS:
    def test_cold_start_actions_use_combined_bound(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.model_pull_timeout_s", 10.0)
        monkeypatch.setattr("app.config.settings.host_start_timeout_s", 5.0)
        from app.services.reconciliation import _action_timeout_s

        assert _action_timeout_s(_action(ActionType.CREATE)) == 75.0
        assert _action_timeout_s(_action(ActionType.MIGRATE)) == 75.0

    def test_quick_actions_keep_60s_bound(self):
        from app.services.reconciliation import _action_timeout_s

        assert _action_timeout_s(_action(ActionType.STOP)) == 60
        assert _action_timeout_s(_action(ActionType.REPLACE)) == 60


class TestAwaitActionWithProgress:
    @pytest.mark.anyio
    async def test_returns_result_when_quick(self):
        result = await _await_action_with_progress(
            _sleep_coro(0.01), _action(ActionType.CREATE), _make_intent()
        )
        assert result == {"done": True}

    @pytest.mark.anyio
    async def test_create_bounded_by_action_timeout_not_60s(self, monkeypatch):
        """The C4 root-cause-A regression: with a short cold-start bound the
        CREATE fails fast (this test fails against the old code, where the
        normal flow was hardcoded to the 60 s _ACTION_TIMEOUT_S)."""
        monkeypatch.setattr("app.config.settings.action_progress_slice_s", 0.02)
        monkeypatch.setattr(
            "app.services.reconciliation._action_timeout_s", lambda a: 0.2
        )
        with (
            patch(
                "app.services.reconciliation._pull_progress_fresh",
                new=AsyncMock(return_value=False),
            ),
            pytest.raises(asyncio.TimeoutError) as excinfo,
        ):
            await _await_action_with_progress(
                _sleep_coro(0.5), _action(ActionType.CREATE), _make_intent()
            )
        assert excinfo.value.recoverable is False

    @pytest.mark.anyio
    async def test_stop_keeps_60s_bound(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.reconciliation._action_timeout_s", lambda a: 0.2
        )
        # STOP is not a cold-start action: the plain 60 s wait_for applies,
        # so a 0.3 s coro completes even though _action_timeout_s was patched.
        result = await _await_action_with_progress(
            _sleep_coro(0.3), _action(ActionType.STOP), _make_intent()
        )
        assert result == {"done": True}

    @pytest.mark.anyio
    async def test_gives_up_while_progress_fresh_marks_recoverable(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.action_progress_slice_s", 0.02)
        monkeypatch.setattr(
            "app.services.reconciliation._action_timeout_s", lambda a: 0.2
        )
        with (
            patch(
                "app.services.reconciliation._pull_progress_fresh",
                new=AsyncMock(return_value=True),
            ),
            pytest.raises(asyncio.TimeoutError) as excinfo,
        ):
            await _await_action_with_progress(
                _sleep_coro(0.5), _action(ActionType.CREATE), _make_intent()
            )
        assert excinfo.value.recoverable is True

    @pytest.mark.anyio
    async def test_action_timeout_error_propagates_without_spinning(self, monkeypatch):
        """A TimeoutError raised *by the action* must not read as a slice expiry.

        asyncio.TimeoutError is the builtin TimeoutError on 3.11+ and aiohttp's
        timeouts subclass it. Catching it as a slice boundary re-awaits an
        already-finished task, which spins at full speed until the ceiling
        (measured at >200k progress checks/second) and replaces the real error
        with a synthetic one.
        """
        monkeypatch.setattr("app.config.settings.action_progress_slice_s", 0.02)
        monkeypatch.setattr(
            "app.services.reconciliation._action_timeout_s", lambda a: 5.0
        )
        sentinel = asyncio.TimeoutError("inner host pull timeout")

        async def _raises_timeout():
            raise sentinel

        progress = AsyncMock(return_value=True)
        with (
            patch("app.services.reconciliation._pull_progress_fresh", new=progress),
            pytest.raises(asyncio.TimeoutError) as excinfo,
        ):
            await _await_action_with_progress(
                _raises_timeout(), _action(ActionType.CREATE), _make_intent()
            )

        # The action's own exception, not a synthesized "exceeded its bound".
        assert excinfo.value is sentinel
        # And no busy-loop: the progress cache is never consulted.
        assert progress.await_count == 0

    @pytest.mark.anyio
    async def test_action_exception_propagates_unchanged(self, monkeypatch):
        """Non-timeout action failures are not reshaped either."""
        monkeypatch.setattr("app.config.settings.action_progress_slice_s", 0.02)

        async def _raises():
            raise ValueError("host said no")

        with pytest.raises(ValueError, match="host said no"):
            await _await_action_with_progress(
                _raises(), _action(ActionType.CREATE), _make_intent()
            )

    @pytest.mark.anyio
    async def test_outer_cancellation_does_not_leak_the_task(self, monkeypatch):
        """Reconciler shutdown must cancel the in-flight action, not detach it."""
        monkeypatch.setattr("app.config.settings.action_progress_slice_s", 0.05)
        monkeypatch.setattr(
            "app.services.reconciliation._action_timeout_s", lambda a: 5.0
        )
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def _long_action():
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with patch(
            "app.services.reconciliation._pull_progress_fresh",
            new=AsyncMock(return_value=True),
        ):
            outer = asyncio.ensure_future(
                _await_action_with_progress(
                    _long_action(), _action(ActionType.CREATE), _make_intent()
                )
            )
            await started.wait()
            outer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await outer

        assert cancelled.is_set()

    @pytest.mark.anyio
    async def test_keeps_waiting_while_progress_is_fresh(self, monkeypatch):
        """A coro that outlives one slice survives while progress is fresh
        and completes within the hard ceiling."""
        monkeypatch.setattr("app.config.settings.action_progress_slice_s", 0.05)
        monkeypatch.setattr(
            "app.services.reconciliation._action_timeout_s", lambda a: 1.0
        )
        with patch(
            "app.services.reconciliation._pull_progress_fresh",
            new=AsyncMock(return_value=True),
        ):
            result = await _await_action_with_progress(
                _sleep_coro(0.3), _action(ActionType.CREATE), _make_intent()
            )
        assert result == {"done": True}


class TestNormalReconcilePath:
    """CREATE through the full _reconcile_one diff/act flow (C4 4.1)."""

    @pytest.mark.anyio
    async def test_timeout_lands_in_last_error_not_bounded_at_60s(self, monkeypatch):
        monkeypatch.setattr("app.config.settings.action_progress_slice_s", 0.02)
        monkeypatch.setattr(
            "app.services.reconciliation._action_timeout_s", lambda a: 0.2
        )
        with patch(
            "app.services.reconciliation._pull_progress_fresh",
            new=AsyncMock(return_value=False),
        ):
            reconciler = Reconciler()
            intent = _make_intent(replicas=1)
            host = _HostStub(id="h1")
            observed = _make_observed(
                managed=[],
                hosts=[host],
                candidates=[(host, _SnapshotStub("h1"))],
            )

            async def _slow_act(*args, **kwargs):
                await asyncio.sleep(0.5)
                return {}

            with (
                patch.object(
                    reconciler, "_observe", new=AsyncMock(return_value=observed)
                ),
                patch.object(reconciler, "_act", new=_slow_act),
                patch.object(
                    reconciler, "_update_status", new=AsyncMock()
                ) as mock_status,
            ):
                await reconciler._reconcile_one(intent)

            call_kwargs = mock_status.call_args[1]
            last_error = call_kwargs.get("last_error")
            assert last_error is not None
            assert last_error["code"] == "TimeoutError"
            assert last_error["recoverable"] is False
