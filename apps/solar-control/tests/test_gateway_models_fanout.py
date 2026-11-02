"""Tests for the bounded parallel fan-out of the models payload."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.gateway import OpenAIGateway


@pytest.mark.anyio
async def test_fanout_runs_in_parallel():
    gw = OpenAIGateway()
    gw.session = AsyncMock()
    gw.session.closed = False

    async def fake(_session, alias, _instances, _semaphore, _timeout):
        await asyncio.sleep(0.05)
        return alias, {alias: {"id": alias}}, {}

    with (
        patch(
            "app.gateway.registry_store.get_registry",
            AsyncMock(return_value={"a": [object()], "b": [object()], "c": [object()]}),
        ),
        patch.object(gw, "_models_for_alias", side_effect=fake),
    ):
        started = time.perf_counter()
        result = await gw._build_available_models(None)
        elapsed = time.perf_counter() - started

    assert elapsed < 0.12  # three 50 ms probes run together
    assert [model["id"] for model in result["data"]] == ["a", "b", "c"]


@pytest.mark.anyio
async def test_fanout_merges_per_alias_payloads():
    gw = OpenAIGateway()
    gw.session = AsyncMock()
    gw.session.closed = False

    async def fake(_session, alias, _instances, _semaphore, _timeout):
        return alias, {alias: {"id": alias}}, {alias: {"name": alias}}

    with (
        patch(
            "app.gateway.registry_store.get_registry",
            AsyncMock(return_value={"a": [object()], "b": [object()]}),
        ),
        patch.object(gw, "_models_for_alias", side_effect=fake),
    ):
        result = await gw._build_available_models(None)

    assert [model["id"] for model in result["data"]] == ["a", "b"]
    assert [model["name"] for model in result["models"]] == ["a", "b"]


@pytest.mark.anyio
async def test_fanout_skips_empty_alias_lists():
    gw = OpenAIGateway()
    gw.session = AsyncMock()
    gw.session.closed = False

    with patch(
        "app.gateway.registry_store.get_registry",
        AsyncMock(return_value={"empty": []}),
    ):
        result = await gw._build_available_models(None)

    assert result == {"models": [], "data": []}
