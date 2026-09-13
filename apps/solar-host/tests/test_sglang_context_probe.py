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
