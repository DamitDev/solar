"""Tests for create-then-stop evacuation (S-043 §4.2, S-057)."""

import copy
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import HTTPException

from app.models import Host, HostStatus
from app.models.migration import MigrationResult
from app.services.migration import (
    delete_instance_on_host,
    execute_evacuation,
    start_instance_on_host,
)


@pytest.fixture
def source_host() -> Host:
    return Host(
        id="host-src",
        name="Source Host",
        url="http://source:8000",
        api_key="key-src",
        status=HostStatus.ONLINE,
    )


@pytest.fixture
def target_host() -> Host:
    return Host(
        id="host-tgt",
        name="Target Host",
        url="http://target:8000",
        api_key="key-tgt",
        status=HostStatus.ONLINE,
        roles=["inference"],
    )


@pytest.fixture
def instance_config() -> dict:
    return {
        "instance_id": "inst-1",
        "config": {
            "alias": "test-model:v1",
            "model_source": "repo://test-model:v1",
            "backend_type": "huggingface_classification",
            "priority": "staging",
            "max_length": 512,
            "labels": ["osl"],
        },
    }


def _evacuation_contexts(source_host, target_host, instance_config):
    """Return (contexts, mocks) for a full evacuation run.

    Patches everything ``execute_evacuation`` touches so only the mocked
    interactions remain; individual tests override side effects.
    """
    from app.services.reconciliation import reconciler

    mock_db = AsyncMock()
    mock_db.get_host = AsyncMock(
        side_effect=lambda hid: (
            source_host
            if hid == "host-src"
            else target_host if hid == "host-tgt" else None
        )
    )
    mock_store = AsyncMock()
    mock_store.get_host_instances = AsyncMock(
        side_effect=lambda hid: [instance_config] if hid == "host-src" else []
    )
    mock_train_check = AsyncMock()
    mock_ensure = AsyncMock(return_value=("/models/repo--test--v1", True))
    mock_create = AsyncMock(
        return_value={"instance": {"id": "new-inst", "status": "stopped"}}
    )
    mock_start = AsyncMock()
    mock_stop = AsyncMock(return_value={"status": "stopped"})
    mock_delete = AsyncMock()
    mock_get = MagicMock()
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"disk": {"available_gb": 100.0}})
    mock_get.return_value.__aenter__.return_value = mock_resp
    mock_intent_db = AsyncMock()
    mock_intent_db.get_intent_by_alias = AsyncMock(
        return_value=SimpleNamespace(id="intent-1")
    )
    # settle_intent is a synchronous deadline write (Reconciler method).
    mock_settle = Mock()

    mocks = {
        "mock_db": mock_db,
        "mock_store": mock_store,
        "mock_train_check": mock_train_check,
        "mock_ensure": mock_ensure,
        "mock_create": mock_create,
        "mock_start": mock_start,
        "mock_stop": mock_stop,
        "mock_delete": mock_delete,
        "mock_get": mock_get,
        "mock_intent_db": mock_intent_db,
        "mock_settle": mock_settle,
    }
    contexts = [
        patch.multiple(
            "app.services.migration",
            host_db=mock_db,
            host_store=mock_store,
            check_no_active_training=mock_train_check,
            ensure_model_on_target=mock_ensure,
            create_instance_on_host=mock_create,
            start_instance_on_host=mock_start,
            stop_source_instance=mock_stop,
            delete_instance_on_host=mock_delete,
        ),
        patch("aiohttp.ClientSession.get", mock_get),
        patch("app.database.intents.intent_db", mock_intent_db),
        patch.object(reconciler, "settle_intent", mock_settle),
    ]
    return contexts, mocks


async def _run(contexts, coro):
    """Await *coro* with all *contexts* entered on one ExitStack."""
    with ExitStack() as stack:
        for c in contexts:
            stack.enter_context(c)
        return await coro


# ── execute_evacuation ───────────────────────────────────────────


@pytest.mark.anyio
async def test_evacuate_happy_path_create_start_then_stop(
    source_host, target_host, instance_config
):
    """The target is created and started before the source is stopped.

    Order: create → start → stop → delete. The settle window is refreshed
    after capture, create, start and delete (four refreshes), so a tick
    can never act on the two-replica overlap.
    """
    contexts, mocks = _evacuation_contexts(source_host, target_host, instance_config)
    calls: list[str] = []

    def _record(name, value=None):
        def _fn(*args, **kwargs):
            calls.append(name)
            return value

        return _fn

    mocks["mock_create"].side_effect = _record(
        "create", {"instance": {"id": "new-inst", "status": "stopped"}}
    )
    mocks["mock_start"].side_effect = _record("start")
    mocks["mock_stop"].side_effect = _record("stop", {"status": "stopped"})
    mocks["mock_delete"].side_effect = _record("delete")

    result = await _run(
        contexts,
        execute_evacuation(
            instance_id="inst-1",
            source_host_id="host-src",
            target_host_id="host-tgt",
        ),
    )

    assert isinstance(result, MigrationResult)
    assert result.status == "completed"
    assert result.alias == "test-model:v1"
    assert result.target_host_id == "host-tgt"
    assert result.target_instance_id == "new-inst"
    assert calls == ["create", "start", "stop", "delete"]
    assert mocks["mock_settle"].call_count == 4
    assert all(
        call.args[0] == "intent-1" for call in mocks["mock_settle"].call_args_list
    )

    # All 10 steps ok, in the create-then-stop order
    step_names = [s.step for s in result.steps]
    assert step_names == [
        "validate_hosts",
        "check_training_jobs",
        "capture_config",
        "validate_target",
        "check_anti_affinity",
        "ensure_model",
        "create_target",
        "start_target",
        "stop_source",
        "delete_source",
    ]
    for step in result.steps:
        assert step.status == "ok", f"Step '{step.step}' failed"


@pytest.mark.anyio
async def test_evacuate_target_keeps_managed_markers(
    source_host, target_host, instance_config
):
    """The target is created managed: markers and priority preserved."""
    captured = {
        **instance_config,
        "managed_by": "intent",
        "intent_id": "intent-1",
    }
    contexts, mocks = _evacuation_contexts(source_host, target_host, captured)
    # Fresh copy per fetch so marker-clearing mutations cannot alias.
    mocks["mock_store"].get_host_instances = AsyncMock(
        side_effect=lambda hid: ([copy.deepcopy(captured)] if hid == "host-src" else [])
    )

    result = await _run(
        contexts,
        execute_evacuation(
            instance_id="inst-1",
            source_host_id="host-src",
            target_host_id="host-tgt",
        ),
    )

    assert result.status == "completed"
    create_wrapper = mocks["mock_create"].call_args[0][1]
    assert create_wrapper["managed_by"] == "intent"
    assert create_wrapper["intent_id"] == "intent-1"
    assert create_wrapper["priority"] == "staging"
    # Evacuation never disowns: the source is deleted, not released.
    step_names = [s.step for s in result.steps]
    assert "disown_source" not in step_names


@pytest.mark.anyio
async def test_evacuate_production_allowed(source_host, target_host, instance_config):
    """Evacuation passes allow_production=True: a drain is the explicit
    policy decision the S-037 safeguard asks for."""
    captured = {**instance_config}
    captured["config"] = {**instance_config["config"], "priority": "production"}
    contexts, _mocks = _evacuation_contexts(source_host, target_host, captured)

    result = await _run(
        contexts,
        execute_evacuation(
            instance_id="inst-1",
            source_host_id="host-src",
            target_host_id="host-tgt",
        ),
    )

    assert result.status == "completed"


@pytest.mark.anyio
async def test_evacuate_create_target_fails(source_host, target_host, instance_config):
    """Create failure: nothing touches the source, failed result."""
    contexts, mocks = _evacuation_contexts(source_host, target_host, instance_config)
    mocks["mock_create"].side_effect = HTTPException(409, "alias conflict")

    result = await _run(
        contexts,
        execute_evacuation(
            instance_id="inst-1",
            source_host_id="host-src",
            target_host_id="host-tgt",
        ),
    )

    assert result.status == "failed"
    assert "Create target failed" in result.error
    assert result.target_instance_id is None
    mocks["mock_stop"].assert_not_called()
    mocks["mock_delete"].assert_not_called()


@pytest.mark.anyio
async def test_evacuate_start_target_fails_deletes_target(
    source_host, target_host, instance_config
):
    """Start failure: the dead target is deleted, the source keeps serving."""
    contexts, mocks = _evacuation_contexts(source_host, target_host, instance_config)
    mocks["mock_start"].side_effect = HTTPException(500, "backend failed")

    result = await _run(
        contexts,
        execute_evacuation(
            instance_id="inst-1",
            source_host_id="host-src",
            target_host_id="host-tgt",
        ),
    )

    assert result.status == "failed"
    assert "Start target failed" in result.error
    mocks["mock_delete"].assert_awaited_once_with(target_host, "new-inst")
    mocks["mock_stop"].assert_not_called()


@pytest.mark.anyio
async def test_evacuate_stop_source_fails_target_left_running(
    source_host, target_host, instance_config
):
    """Stop failure: the running target stays; the result names it so the
    reconciler's surplus logic can converge on its own."""
    contexts, mocks = _evacuation_contexts(source_host, target_host, instance_config)
    mocks["mock_stop"].side_effect = HTTPException(500, "stop failed")

    result = await _run(
        contexts,
        execute_evacuation(
            instance_id="inst-1",
            source_host_id="host-src",
            target_host_id="host-tgt",
        ),
    )

    assert result.status == "failed"
    assert "Stop source failed" in result.error
    assert result.target_instance_id == "new-inst"
    mocks["mock_delete"].assert_not_called()


@pytest.mark.anyio
async def test_evacuate_delete_source_fails(source_host, target_host, instance_config):
    """Delete failure: failed result with the target running. The source
    stays owned (no disown), so the next drain tick's STOP retries."""
    contexts, mocks = _evacuation_contexts(source_host, target_host, instance_config)
    mocks["mock_delete"].side_effect = HTTPException(500, "delete failed")

    result = await _run(
        contexts,
        execute_evacuation(
            instance_id="inst-1",
            source_host_id="host-src",
            target_host_id="host-tgt",
        ),
    )

    assert result.status == "failed"
    assert "Delete source failed" in result.error
    assert result.target_instance_id == "new-inst"


@pytest.mark.anyio
async def test_evacuate_ensure_model_fails(source_host, target_host, instance_config):
    """Model pull failure: nothing created, nothing stopped."""
    contexts, mocks = _evacuation_contexts(source_host, target_host, instance_config)
    mocks["mock_ensure"].side_effect = HTTPException(507, "no disk")

    result = await _run(
        contexts,
        execute_evacuation(
            instance_id="inst-1",
            source_host_id="host-src",
            target_host_id="host-tgt",
        ),
    )

    assert result.status == "failed"
    assert "Ensure model failed" in result.error
    mocks["mock_create"].assert_not_called()
    mocks["mock_stop"].assert_not_called()
    mocks["mock_delete"].assert_not_called()


@pytest.mark.anyio
async def test_evacuate_same_host_rejected(source_host):
    with (
        patch("app.services.migration.host_db") as mock_db,
        pytest.raises(HTTPException) as exc,
    ):
        mock_db.get_host = AsyncMock(return_value=source_host)
        await execute_evacuation(
            instance_id="inst-1",
            source_host_id="host-src",
            target_host_id="host-src",
        )
    assert exc.value.status_code == 422


@pytest.mark.anyio
async def test_evacuate_source_host_not_found(target_host):
    with patch("app.services.migration.host_db") as mock_db:
        mock_db.get_host = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc:
            await execute_evacuation(
                instance_id="inst-1",
                source_host_id="host-missing",
                target_host_id="host-tgt",
            )
    assert exc.value.status_code == 404


# ── start_instance_on_host ───────────────────────────────────────


@pytest.mark.anyio
async def test_start_instance_on_host_success(target_host):
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_post.return_value.__aenter__.return_value = mock_resp

        await start_instance_on_host(target_host, "inst-1")

    url = mock_post.call_args[0][0]
    assert url == "http://target:8000/instances/inst-1/start"


@pytest.mark.anyio
async def test_start_instance_on_host_non_200_raises(target_host):
    with patch("aiohttp.ClientSession.post") as mock_post:
        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="backend exploded")
        mock_post.return_value.__aenter__.return_value = mock_resp

        with pytest.raises(HTTPException) as exc:
            await start_instance_on_host(target_host, "inst-1")
    assert exc.value.status_code == 500


@pytest.mark.anyio
async def test_start_instance_on_host_unreachable(target_host):
    with (
        patch(
            "aiohttp.ClientSession.post",
            side_effect=ConnectionError("refused"),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await start_instance_on_host(target_host, "inst-1")
    assert exc.value.status_code == 502


# ── delete_instance_on_host ──────────────────────────────────────


@pytest.mark.anyio
async def test_delete_instance_on_host_success(target_host):
    with patch("aiohttp.ClientSession.delete") as mock_delete:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_delete.return_value.__aenter__.return_value = mock_resp

        await delete_instance_on_host(target_host, "inst-1")

    url = mock_delete.call_args[0][0]
    assert url == "http://target:8000/instances/inst-1"


@pytest.mark.anyio
async def test_delete_instance_on_host_unreachable(target_host):
    with (
        patch(
            "aiohttp.ClientSession.delete",
            side_effect=ConnectionError("refused"),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await delete_instance_on_host(target_host, "inst-1")
    assert exc.value.status_code == 502
