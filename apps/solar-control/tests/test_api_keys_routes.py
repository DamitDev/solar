"""Tests for the /api/api-keys management router (S-045)."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.database.api_keys import ApiKey
from app.routes.management.api_keys import (
    create_api_key,
    delete_api_key,
    list_api_keys,
    rotate_api_key,
    update_api_key,
)

pytestmark = pytest.mark.anyio


class CreateRequest:
    def __init__(self, endpoint_id: str, name: str, description: str | None = None):
        self.endpoint_id = endpoint_id
        self.name = name
        self.description = description


class UpdateRequest:
    def __init__(
        self,
        name: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
        endpoint_id: str | None = None,
    ):
        self.name = name
        self.description = description
        self.enabled = enabled
        self.endpoint_id = endpoint_id
        self._description_provided = description is not None


def _key(endpoint_id: str = "ep-1", key_id: str = "key-1", name: str = "default"):
    return ApiKey(
        id=key_id,
        endpoint_id=endpoint_id,
        name=name,
        key="sk-test-123",
        enabled=True,
    )


@pytest.fixture(autouse=True)
def _noop_side_effects():
    """Keep cache invalidation and socket events inert in unit tests."""
    with (
        patch(
            "app.routes.management.api_keys.invalidate_endpoint_cache",
            new=AsyncMock(),
        ),
        patch("app.routes.management.api_keys._emit_api_keys_update", new=AsyncMock()),
    ):
        yield


@pytest.fixture
def dbs():
    """The DB singletons used by the route handlers (AsyncMock so their
    attributes are awaitable)."""
    with (
        patch("app.routes.management.api_keys.endpoint_db", new=AsyncMock()) as mock_ep,
        patch(
            "app.routes.management.api_keys.api_key_db", new=AsyncMock()
        ) as mock_keys,
    ):
        yield mock_ep, mock_keys


async def test_list_api_keys(dbs):
    _, mock_keys = dbs
    mock_keys.list_all.return_value = [_key(), _key("ep-2", "key-2", "secondary")]
    result = await list_api_keys()
    assert len(result) == 2
    assert result[0]["endpoint_id"] == "ep-1"


async def test_list_api_keys_filtered_by_endpoint(dbs):
    mock_ep, mock_keys = dbs
    mock_ep.get_endpoint.return_value = object()
    mock_keys.list_for_endpoint.return_value = [_key()]
    result = await list_api_keys(endpoint_id="ep-1")
    mock_keys.list_for_endpoint.assert_awaited_once_with("ep-1")
    assert len(result) == 1


async def test_list_api_keys_unknown_endpoint_404(dbs):
    mock_ep, _ = dbs
    mock_ep.get_endpoint.return_value = None
    with pytest.raises(HTTPException) as exc:
        await list_api_keys(endpoint_id="missing")
    assert exc.value.status_code == 404


async def test_create_api_key(dbs):
    mock_ep, mock_keys = dbs
    mock_ep.get_endpoint.return_value = object()
    mock_keys.create.return_value = _key(name="ci")
    result = await create_api_key(CreateRequest("ep-1", "ci", "CI runner"))
    assert result["name"] == "ci"
    mock_keys.create.assert_awaited()


async def test_create_api_key_unknown_endpoint_404(dbs):
    mock_ep, _ = dbs
    mock_ep.get_endpoint.return_value = None
    with pytest.raises(HTTPException) as exc:
        await create_api_key(CreateRequest("ep-1", "ci"))
    assert exc.value.status_code == 404


async def test_create_api_key_duplicate_name_409(dbs):
    mock_ep, mock_keys = dbs
    mock_ep.get_endpoint.return_value = object()
    mock_keys.create.side_effect = Exception("duplicate key value violates unique")
    with pytest.raises(HTTPException) as exc:
        await create_api_key(CreateRequest("ep-1", "ci"))
    assert exc.value.status_code == 409
    assert "already exists" in exc.value.detail


async def test_update_api_key_rename(dbs):
    _, mock_keys = dbs
    mock_keys.update.return_value = _key(name="new-name")
    result = await update_api_key("key-1", UpdateRequest(name="new-name"))
    assert result["name"] == "new-name"
    assert mock_keys.update.await_args.kwargs["name"] == "new-name"


async def test_update_api_key_description(dbs):
    _, mock_keys = dbs
    mock_keys.update.return_value = _key()
    await update_api_key("key-1", UpdateRequest(description="updated"))
    assert mock_keys.update.await_args.kwargs["description"] == "updated"


async def test_update_api_key_toggle_enabled(dbs):
    _, mock_keys = dbs
    mock_keys.update.return_value = _key()
    await update_api_key("key-1", UpdateRequest(enabled=False))
    assert mock_keys.update.await_args.kwargs["enabled"] is False


async def test_update_api_key_reassign_endpoint(dbs):
    mock_ep, mock_keys = dbs
    mock_ep.get_endpoint.return_value = object()
    mock_keys.update.return_value = _key("ep-2")
    await update_api_key("key-1", UpdateRequest(endpoint_id="ep-2"))
    mock_ep.get_endpoint.assert_awaited_once_with("ep-2")
    assert mock_keys.update.await_args.kwargs["endpoint_id"] == "ep-2"


async def test_update_api_key_reassign_missing_endpoint_404(dbs):
    mock_ep, _ = dbs
    mock_ep.get_endpoint.return_value = None
    with pytest.raises(HTTPException) as exc:
        await update_api_key("key-1", UpdateRequest(endpoint_id="ep-2"))
    assert exc.value.status_code == 404


async def test_update_api_key_conflict_409(dbs):
    _, mock_keys = dbs
    mock_keys.update.side_effect = Exception("unique constraint")
    with pytest.raises(HTTPException) as exc:
        await update_api_key("key-1", UpdateRequest(name="taken"))
    assert exc.value.status_code == 409


async def test_update_api_key_not_found(dbs):
    _, mock_keys = dbs
    mock_keys.update.return_value = None
    with pytest.raises(HTTPException) as exc:
        await update_api_key("nope", UpdateRequest(name="x"))
    assert exc.value.status_code == 404


async def test_rotate_api_key(dbs):
    _, mock_keys = dbs
    rotated = _key()
    rotated.key = "sk-new-material"
    mock_keys.rotate.return_value = rotated
    result = await rotate_api_key("key-1")
    assert result["key"] == "sk-new-material"
    mock_keys.rotate.assert_awaited_once_with("key-1")


async def test_rotate_missing_key_404(dbs):
    _, mock_keys = dbs
    mock_keys.rotate.return_value = None
    with pytest.raises(HTTPException) as exc:
        await rotate_api_key("nope")
    assert exc.value.status_code == 404


async def test_delete_api_key(dbs):
    _, mock_keys = dbs
    mock_keys.get.return_value = _key(name="primary-key")
    result = await delete_api_key("key-1")
    assert result["id"] == "key-1"
    mock_keys.delete.assert_awaited_once_with("key-1")


async def test_delete_missing_key_404(dbs):
    _, mock_keys = dbs
    mock_keys.get.return_value = None
    with pytest.raises(HTTPException) as exc:
        await delete_api_key("nope")
    assert exc.value.status_code == 404
