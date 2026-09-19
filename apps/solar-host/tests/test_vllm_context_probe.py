"""Tests for the vLLM context-size probe."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from solar_host.backends.vllm import VllmRunner
from solar_host.models.base import Instance, InstanceStatus


def _instance(port: int | None):
    return Instance(
        id="i-test",
        config=MagicMock(alias="test:8b"),
        status=InstanceStatus.RUNNING,
        port=port,
    )


def _session_returning(resp) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


def _response(payload=None, status: int = 200):
    resp = AsyncMock()
    resp.status = status
    if payload is not None:
        resp.json = AsyncMock(return_value=payload)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


class TestProbeContextSize:
    @pytest.mark.anyio
    async def test_parses_max_model_len_from_the_model_listing(self):
        runner = VllmRunner()
        instance = _instance(3501)
        payload = {
            "object": "list",
            "data": [
                {"id": "test:8b", "root": "/models/test", "max_model_len": 1048576}
            ],
        }

        with patch(
            "aiohttp.ClientSession", return_value=_session_returning(_response(payload))
        ):
            size = await runner.probe_context_size(instance)

        assert size == 1048576

    @pytest.mark.anyio
    async def test_skips_entries_without_a_usable_length(self):
        runner = VllmRunner()
        instance = _instance(3501)
        payload = {
            "data": [
                {"id": "first", "max_model_len": "huge"},
                {"id": "second", "max_model_len": 262144},
            ]
        }

        with patch(
            "aiohttp.ClientSession", return_value=_session_returning(_response(payload))
        ):
            size = await runner.probe_context_size(instance)

        assert size == 262144

    @pytest.mark.anyio
    async def test_no_port_returns_none(self):
        runner = VllmRunner()
        assert await runner.probe_context_size(_instance(None)) is None

    @pytest.mark.anyio
    async def test_http_error_returns_none(self):
        runner = VllmRunner()

        with patch(
            "aiohttp.ClientSession",
            return_value=_session_returning(_response(status=401)),
        ):
            size = await runner.probe_context_size(_instance(3501))

        assert size is None

    @pytest.mark.anyio
    async def test_garbage_payload_returns_none(self):
        runner = VllmRunner()

        with patch(
            "aiohttp.ClientSession",
            return_value=_session_returning(_response({"data": "not-a-list"})),
        ):
            size = await runner.probe_context_size(_instance(3501))

        assert size is None

    @pytest.mark.anyio
    async def test_sends_host_api_key(self):
        """The probe must authenticate: /v1 is behind the auth middleware."""
        runner = VllmRunner()
        instance = _instance(3501)
        payload = {"data": [{"id": "test:8b", "max_model_len": 4096}]}
        mock_resp = _response(payload)
        session = MagicMock()
        captured = {}
        session.get = MagicMock(
            side_effect=lambda url, **kwargs: (
                captured.update(url=url, **kwargs),
                mock_resp,
            )[1]
        )
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("aiohttp.ClientSession", return_value=session),
            patch("solar_host.backends.vllm.settings") as mock_settings,
        ):
            mock_settings.api_key = "solar_test_key"
            await runner.probe_context_size(instance)

        assert captured["url"].endswith("/v1/models")
        assert captured["headers"]["Authorization"] == "Bearer solar_test_key"
