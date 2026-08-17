"""Tests for the /api/endpoints management router.

The update tests build request models through ``model_validate`` so that
``model_fields_set`` reflects a real JSON body: an omitted key must leave the
column untouched while an explicit value (including ``null``) must be written.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.database.endpoints import ApiEndpoint
from app.routes.management.endpoints import (
    EndpointCreate,
    EndpointModelPreview,
    EndpointUpdate,
    create_endpoint,
    get_endpoint_models,
    preview_models,
    update_endpoint,
)

pytestmark = pytest.mark.anyio


def _endpoint(**overrides) -> ApiEndpoint:
    return ApiEndpoint(
        id=overrides.pop("id", "ep-1"),
        name=overrides.pop("name", "primary"),
        **overrides,
    )


@pytest.fixture(autouse=True)
def _noop_side_effects():
    """Keep cache invalidation and socket events inert in unit tests."""
    with (
        patch(
            "app.routes.management.endpoints.invalidate_endpoint_cache",
            new=AsyncMock(),
        ),
        patch(
            "app.routes.management.endpoints._emit_endpoints_update",
            new=AsyncMock(),
        ),
    ):
        yield


@pytest.fixture
def dbs():
    with (
        patch(
            "app.routes.management.endpoints.endpoint_db", new=AsyncMock()
        ) as mock_ep,
        patch(
            "app.routes.management.endpoints.api_key_db", new=AsyncMock()
        ) as mock_keys,
    ):
        mock_keys.list_for_endpoint.return_value = []
        yield mock_ep, mock_keys


@pytest.fixture
def registry():
    with patch(
        "app.routes.management.endpoints.registry_store", new=AsyncMock()
    ) as store:
        store.get_registry.return_value = {
            "iris-osl:8b": [{"host": "a"}],
            "iris-osl:70b": [{"host": "b"}],
            "qwen-v4-flash:284b": [{"host": "c"}],
            "unloaded:1b": [],
        }
        yield store


async def test_create_endpoint_persists_patterns(dbs):
    mock_ep, _ = dbs
    mock_ep.create_endpoint.return_value = _endpoint(
        serve_all_models=False, model_patterns=["iris-osl:*"]
    )
    result = await create_endpoint(
        EndpointCreate.model_validate(
            {
                "name": "scoped",
                "serve_all_models": False,
                "model_patterns": ["iris-osl:*"],
            }
        )
    )
    assert mock_ep.create_endpoint.await_args.kwargs["model_patterns"] == ["iris-osl:*"]
    assert result["model_patterns"] == ["iris-osl:*"]
    assert result["key_count"] == 0


async def test_update_endpoint_persists_model_patterns(dbs):
    """Regression: the patterns used to be dropped on every PUT."""
    mock_ep, _ = dbs
    mock_ep.update_endpoint.return_value = _endpoint(
        serve_all_models=False, model_patterns=["iris-*"]
    )
    await update_endpoint(
        "ep-1",
        EndpointUpdate.model_validate(
            {"serve_all_models": False, "model_patterns": ["iris-*"]}
        ),
    )
    kwargs = mock_ep.update_endpoint.await_args.kwargs
    assert kwargs["model_patterns"] == ["iris-*"]
    assert kwargs["serve_all_models"] is False


async def test_update_endpoint_clears_model_patterns_with_empty_list(dbs):
    mock_ep, _ = dbs
    mock_ep.update_endpoint.return_value = _endpoint()
    await update_endpoint("ep-1", EndpointUpdate.model_validate({"model_patterns": []}))
    assert mock_ep.update_endpoint.await_args.kwargs["model_patterns"] == []


async def test_update_endpoint_omitted_patterns_are_untouched(dbs):
    mock_ep, _ = dbs
    mock_ep.update_endpoint.return_value = _endpoint(name="renamed")
    await update_endpoint("ep-1", EndpointUpdate.model_validate({"name": "renamed"}))
    kwargs = mock_ep.update_endpoint.await_args.kwargs
    assert "model_patterns" not in kwargs
    assert "description" not in kwargs
    assert kwargs["name"] == "renamed"


async def test_update_endpoint_clears_description_when_explicitly_null(dbs):
    mock_ep, _ = dbs
    mock_ep.update_endpoint.return_value = _endpoint()
    await update_endpoint("ep-1", EndpointUpdate.model_validate({"description": None}))
    assert mock_ep.update_endpoint.await_args.kwargs["description"] is None


async def test_update_endpoint_not_found_404(dbs):
    mock_ep, _ = dbs
    mock_ep.update_endpoint.return_value = None
    with pytest.raises(HTTPException) as exc:
        await update_endpoint("nope", EndpointUpdate.model_validate({"name": "x"}))
    assert exc.value.status_code == 404


async def test_preview_models_filters_and_reports_available(registry):
    result = await preview_models(
        EndpointModelPreview(serve_all_models=False, model_patterns=["iris-osl:*"])
    )
    assert result["aliases"] == ["iris-osl:8b", "iris-osl:70b"]
    assert result["count"] == 2
    # Aliases without a live instance are not offered.
    assert result["available"] == ["iris-osl:8b", "iris-osl:70b", "qwen-v4-flash:284b"]


async def test_preview_models_serve_all_returns_every_alias(registry):
    result = await preview_models(
        EndpointModelPreview(serve_all_models=True, model_patterns=["iris-osl:*"])
    )
    assert result["aliases"] == result["available"]
    assert result["count"] == 3


async def test_endpoint_models_uses_stored_patterns(dbs, registry):
    mock_ep, _ = dbs
    mock_ep.get_endpoint.return_value = _endpoint(
        serve_all_models=False, model_patterns=["qwen-*"]
    )
    result = await get_endpoint_models("ep-1")
    assert result["aliases"] == ["qwen-v4-flash:284b"]
    assert result["count"] == 1
