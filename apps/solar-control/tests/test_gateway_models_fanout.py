"""Tests for the bounded parallel fan-out of the models payload."""

import asyncio
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from app.gateway import OpenAIGateway
from app.models import RegistryEntry


@pytest.mark.anyio
async def test_fanout_runs_in_parallel():
    gw = OpenAIGateway()
    gw.session = AsyncMock()
    gw.session.closed = False
    in_flight = 0
    max_in_flight = 0

    async def fake(_session, alias, _instances, _semaphore, _timeout):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return alias, {alias: {"id": alias}}, {}

    with (
        patch(
            "app.gateway.registry_store.get_registry",
            AsyncMock(return_value={"a": [object()], "b": [object()], "c": [object()]}),
        ),
        patch.object(gw, "_models_for_alias", side_effect=fake),
    ):
        result = await gw._build_available_models(None)

    assert max_in_flight == 3  # all three probes were in flight together
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


class _FakeResponse:
    def __init__(self, session, alias, *, error=None, delay=0.0):
        self.session = session
        self.alias = alias
        self.error = error
        self.delay = delay

    async def __aenter__(self):
        self.session.in_flight += 1
        self.session.max_in_flight = max(
            self.session.max_in_flight, self.session.in_flight
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            self.session.in_flight -= 1
            raise self.error
        return self

    async def __aexit__(self, *_args):
        self.session.in_flight -= 1
        return False

    async def json(self):
        return {
            "models": [{"name": self.alias}],
            "data": [{"id": self.alias}],
        }


class _FakeSession:
    def __init__(self, errors=None, delay=0.0):
        self.errors = errors or {}
        self.delay = delay
        self.in_flight = 0
        self.max_in_flight = 0

    def get(self, url, **_kwargs):
        alias = url.split("//", 1)[1].split(":", 1)[0]
        return _FakeResponse(
            self, alias, error=self.errors.get(alias), delay=self.delay
        )


def _entry(alias: str) -> RegistryEntry:
    return RegistryEntry(
        host_id="host-1",
        instance_id=f"inst-{alias}",
        url=f"http://{alias}:8000",
        api_key="sk-test",
        model_alias=alias,
        context_size=8192,
    )


@pytest.mark.anyio
async def test_models_for_alias_falls_back_on_a_timeout():
    gw = OpenAIGateway()
    session = _FakeSession(errors={"bad": asyncio.TimeoutError()})
    semaphore = asyncio.Semaphore(2)
    timeout = aiohttp.ClientTimeout(total=0.01)

    alias, data, ollama = await gw._models_for_alias(
        session, "bad", [_entry("bad")], semaphore, timeout
    )

    assert alias == "bad"
    assert data["bad"]["id"] == "bad"
    assert ollama["bad"]["name"] == "bad"


@pytest.mark.anyio
async def test_models_for_alias_respects_the_concurrency_ceiling():
    gw = OpenAIGateway()
    session = _FakeSession(delay=0.02)
    semaphore = asyncio.Semaphore(2)
    timeout = aiohttp.ClientTimeout(total=5)

    await asyncio.gather(
        *(
            gw._models_for_alias(session, alias, [_entry(alias)], semaphore, timeout)
            for alias in ("a", "b", "c", "d", "e", "f")
        )
    )

    assert session.max_in_flight == 2
