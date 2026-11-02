"""Tests for the cached models payload on the gateway."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.gateway import OpenAIGateway
from app.services.model_list_cache import model_list_cache


@pytest.fixture(autouse=True)
def _clear_cache():
    model_list_cache.invalidate()
    yield
    model_list_cache.invalidate()


@pytest.mark.anyio
async def test_models_list_is_cached_per_scope():
    gw = OpenAIGateway()
    build = AsyncMock(return_value={"models": [], "data": []})
    with patch.object(gw, "_build_available_models", build):
        assert await gw.get_available_models(["wind"]) == {"models": [], "data": []}
        assert await gw.get_available_models(["wind"]) == {"models": [], "data": []}
        assert build.await_count == 1
        await gw.get_available_models(["core"])
        assert build.await_count == 2


@pytest.mark.anyio
async def test_models_list_single_flight():
    gw = OpenAIGateway()
    gate = asyncio.Event()
    calls = 0

    async def slow(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        await gate.wait()
        return {"models": [], "data": []}

    with patch.object(gw, "_build_available_models", side_effect=slow):
        tasks = [
            asyncio.create_task(gw.get_available_models(["wind"])) for _ in range(5)
        ]
        await asyncio.sleep(0.01)
        gate.set()
        results = await asyncio.gather(*tasks)

    assert calls == 1
    assert all(result == {"models": [], "data": []} for result in results)
