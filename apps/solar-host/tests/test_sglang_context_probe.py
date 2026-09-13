"""Tests for the SGLang context-size probe (S-060 follow-up)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from solar_host.backends.sglang import SglangRunner
from solar_host.models.base import Instance, InstanceStatus


def _instance(port: int | None):
    return Instance(
        id="i-test",
        config=MagicMock(alias="test:8b"),
        status=InstanceStatus.RUNNING,
        port=port,
    )


class TestProbeContextSize:
    @pytest.mark.anyio
    async def test_parses_context_length(self):
        runner = SglangRunner()
        instance = _instance(30000)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"context_length": 262144})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            size = await runner.probe_context_size(instance)

        assert size == 262144

    @pytest.mark.anyio
    async def test_no_port_returns_none(self):
        runner = SglangRunner()
        assert await runner.probe_context_size(_instance(None)) is None

    @pytest.mark.anyio
    async def test_http_error_returns_none(self):
        runner = SglangRunner()
        instance = _instance(30000)

        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            size = await runner.probe_context_size(instance)
        assert size is None

    @pytest.mark.anyio
    async def test_garbage_payload_returns_none(self):
        runner = SglangRunner()
        instance = _instance(30000)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"context_length": "huge"})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            size = await runner.probe_context_size(instance)
        assert size is None

    @pytest.mark.anyio
    async def test_falls_back_to_get_model_info(self):
        """First endpoint 404s -> second endpoint carries the key (S-060 #71)."""
        runner = SglangRunner()
        instance = _instance(30000)

        not_found = AsyncMock()
        not_found.status = 404
        not_found.__aenter__ = AsyncMock(return_value=not_found)
        not_found.__aexit__ = AsyncMock(return_value=False)

        found = AsyncMock()
        found.status = 200
        found.json = AsyncMock(return_value={"context_length": 262144})
        found.__aenter__ = AsyncMock(return_value=found)
        found.__aexit__ = AsyncMock(return_value=False)

        responses = {
            "http://127.0.0.1:30000/get_server_info": not_found,
            "http://127.0.0.1:30000/get_model_info": found,
        }
        requested = []

        def get(_url, **_kwargs):
            requested.append(_url)
            return responses[_url]

        mock_session = MagicMock()
        mock_session.get = get
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            size = await runner.probe_context_size(instance)

        assert requested == [
            "http://127.0.0.1:30000/get_server_info",
            "http://127.0.0.1:30000/get_model_info",
        ]
        assert size == 262144

    @pytest.mark.anyio
    async def test_sends_host_api_key(self):
        """The probe must authenticate: SGLang runs with --api-key (401 otherwise)."""
        runner = SglangRunner()
        instance = _instance(30000)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"context_length": 4096})
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        captured = {}
        mock_session.get = MagicMock(
            side_effect=lambda url, **kwargs: (captured.update(kwargs), mock_resp)[1]
        )
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("aiohttp.ClientSession", return_value=mock_session),
            patch("solar_host.backends.sglang.settings") as mock_settings,
        ):
            mock_settings.api_key = "solar_test_key"
            await runner.probe_context_size(instance)

        assert captured["headers"]["Authorization"] == "Bearer solar_test_key"
