"""Start/restart proxy and reconciler start calls must use the configured
host start timeout.

A blocking instance start answers only once the backend reports readiness
(log-gated), which can take minutes for a cold model load — every hop of
the call chain must fit inside ``settings.host_start_timeout_s``.
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.models import Host, HostStatus


@pytest.fixture
def mock_host():
    return Host(
        id="host-1",
        name="Test Host",
        url="http://test-host:8000",
        api_key="test-key",
        status=HostStatus.ONLINE,
    )


@pytest.mark.anyio
async def test_start_route_proxies_with_host_start_timeout():
    from app.routes.management.hosts import start_instance

    with patch(
        "app.routes.management.hosts._proxy_instance_action",
        new=AsyncMock(return_value={}),
    ) as mock_proxy:
        await start_instance("host-1", "inst-1")
        mock_proxy.assert_awaited_once_with(
            "host-1", "inst-1", "start", timeout=settings.host_start_timeout_s
        )


@pytest.mark.anyio
async def test_restart_route_proxies_with_host_start_timeout():
    from app.routes.management.hosts import restart_instance

    with patch(
        "app.routes.management.hosts._proxy_instance_action",
        new=AsyncMock(return_value={}),
    ) as mock_proxy:
        await restart_instance("host-1", "inst-1")
        mock_proxy.assert_awaited_once_with(
            "host-1", "inst-1", "restart", timeout=settings.host_start_timeout_s
        )


@pytest.mark.anyio
async def test_proxy_instance_action_passes_timeout_to_aiohttp(mock_host):
    from app.routes.management.hosts import _proxy_instance_action

    with (
        patch(
            "app.database.hosts.host_db.get_host", new=AsyncMock(return_value=mock_host)
        ),
        patch("aiohttp.ClientSession.post") as mock_post,
    ):
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = {"status": "running"}
        mock_post.return_value.__aenter__.return_value = mock_resp

        await _proxy_instance_action(
            "host-1", "inst-1", "start", timeout=settings.host_start_timeout_s
        )
        kwargs = mock_post.call_args.kwargs
        assert kwargs["timeout"].total == settings.host_start_timeout_s


@pytest.mark.anyio
async def test_reconciler_start_uses_host_start_timeout(mock_host, monkeypatch):
    from app.services.reconciliation import reconciler

    monkeypatch.setattr("app.config.settings.host_start_timeout_s", 1234.5)

    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_post.return_value.__aenter__.return_value = mock_resp

        await reconciler._start_instance(mock_host, "inst-1")
        kwargs = mock_post.call_args.kwargs
        assert kwargs["timeout"].total == 1234.5


# ── C2: structured start-failure plumbing ───────────────────────


def _mock_failed_start(body):
    """Mock POST /instances/{id}/start answering HTTP 500 with *body*."""
    mock_resp = AsyncMock()
    mock_resp.status = 500
    mock_resp.text.return_value = body
    return patch(
        "aiohttp.ClientSession.post",
        new=lambda *a, **kw: _AsyncCtx(mock_resp),
    )


class _AsyncCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


@pytest.mark.anyio
async def test_structured_failure_body_carries_instance_details(mock_host):
    """A JSON failure body yields instance_id, exit_code and log_tail."""
    from app.services.reconciliation import InstanceStartFailed, reconciler

    body = (
        '{"detail": "Failed to start instance: Process exited unexpectedly '
        '(exit code: 1)", "instance_id": "inst-1", "exit_code": 1, '
        '"log_tail": ["loading model", "fatal: bad config"]}'
    )
    with _mock_failed_start(body), pytest.raises(InstanceStartFailed) as excinfo:
        await reconciler._start_instance(mock_host, "inst-1")

    exc = excinfo.value
    assert exc.status_code == 502
    assert exc.instance_id == "inst-1"
    assert exc.exit_code == 1
    assert exc.log_tail == ["loading model", "fatal: bad config"]
    # The human-readable detail is preserved for backwards compatibility.
    assert "inst-1" in exc.detail


@pytest.mark.anyio
async def test_nested_failure_body_is_unwrapped(mock_host):
    """A host that raised the payload as an HTTPException detail still parses.

    FastAPI wraps ``HTTPException.detail`` in its own ``detail`` key, so such a
    host puts the diagnostic fields one level deeper. Hosts deploy separately
    from control, so both shapes have to work.
    """
    from app.services.reconciliation import InstanceStartFailed, reconciler

    body = (
        '{"detail": {"detail": "Failed to start instance: Process exited '
        'unexpectedly (exit code: 3)", "instance_id": "inst-1", '
        '"exit_code": 3, "log_tail": ["boom"]}}'
    )
    with _mock_failed_start(body), pytest.raises(InstanceStartFailed) as excinfo:
        await reconciler._start_instance(mock_host, "inst-1")

    exc = excinfo.value
    assert exc.instance_id == "inst-1"
    assert exc.exit_code == 3
    assert exc.log_tail == ["boom"]
    # Never leak a dict repr into the human-readable detail.
    assert "{" not in exc.detail
    assert "Process exited unexpectedly" in exc.detail


@pytest.mark.anyio
async def test_legacy_dict_body_with_string_detail(mock_host):
    """A pre-C2 host answers {"detail": "..."} — no structured fields."""
    from app.services.reconciliation import InstanceStartFailed, reconciler

    body = '{"detail": "Failed to start instance: Process exited unexpectedly"}'
    with _mock_failed_start(body), pytest.raises(InstanceStartFailed) as excinfo:
        await reconciler._start_instance(mock_host, "inst-1")

    exc = excinfo.value
    assert "Process exited unexpectedly" in exc.detail
    assert exc.instance_id == "inst-1"  # falls back to the requested id
    assert exc.exit_code is None
    assert exc.log_tail is None


def test_parse_start_failure_body_rejects_malformed_fields():
    """Wrong-typed fields degrade to None rather than propagating garbage."""
    from app.services.reconciliation import _parse_start_failure_body

    message, instance_id, exit_code, log_tail = _parse_start_failure_body(
        '{"detail": 42, "instance_id": "", "exit_code": true, "log_tail": "nope"}'
    )
    # A non-string detail falls back to the raw body rather than a repr.
    assert isinstance(message, str)
    assert instance_id is None
    # bool is an int subclass, but an exit code is never a boolean.
    assert exit_code is None
    assert log_tail is None


@pytest.mark.anyio
async def test_plain_string_body_keeps_legacy_502(mock_host):
    """Older hosts send a plain string; the legacy 502 detail survives."""
    from app.services.reconciliation import InstanceStartFailed, reconciler

    with (
        _mock_failed_start("Process exited unexpectedly (exit code: 1)"),
        pytest.raises(InstanceStartFailed) as excinfo,
    ):
        await reconciler._start_instance(mock_host, "inst-1")

    exc = excinfo.value
    assert exc.status_code == 502
    assert "Process exited unexpectedly" in exc.detail
    # No structured body: instance_id falls back to the action's id and no
    # log tail is attached (backwards-compatible 502).
    assert exc.instance_id == "inst-1"
    assert exc.log_tail is None
    assert exc.exit_code is None


@pytest.mark.anyio
async def test_last_error_carries_structured_fields():
    """The reconciler's last_error mapping populates instance_id + log_tail."""
    from unittest.mock import patch as _patch

    from test_reconciliation import (
        _HostStub,
        _make_intent,
        _make_observed,
        _SnapshotStub,
    )

    from app.services.reconciliation import (
        InstanceStartFailed,
        Reconciler,
    )

    reconciler = Reconciler()
    intent = _make_intent(replicas=1)
    host = _HostStub(id="h1")
    observed = _make_observed(
        managed=[],
        hosts=[host],
        candidates=[(host, _SnapshotStub("h1"))],
    )

    def _fail(*args, **kwargs):
        raise InstanceStartFailed(
            detail="failed",
            instance_id="inst-9",
            exit_code=3,
            log_tail=["boom"],
        )

    with (
        _patch.object(reconciler, "_observe", new=AsyncMock(return_value=observed)),
        _patch.object(reconciler, "_act", new=_fail),
        _patch.object(reconciler, "_update_status", new=AsyncMock()) as mock_status,
    ):
        await reconciler._reconcile_one(intent)

    last_error = mock_status.call_args[1]["last_error"]
    assert last_error["code"] == "InstanceStartFailed"
    assert last_error["instance_id"] == "inst-9"
    assert last_error["log_tail"] == ["boom"]
    assert last_error["recoverable"] is False
