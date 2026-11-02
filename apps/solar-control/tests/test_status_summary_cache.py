"""Tests for the full status summary response cache."""

from unittest.mock import AsyncMock, patch

import pytest

from app.services.status_summary import StatusSummaryService


@pytest.mark.anyio
async def test_build_reuses_the_response_within_ttl():
    svc = StatusSummaryService()
    build = AsyncMock(return_value="payload")
    with patch.object(svc, "_build", build):
        assert await svc.build() == "payload"
        assert await svc.build() == "payload"
        assert build.await_count == 1


@pytest.mark.anyio
async def test_clear_cache_forces_a_rebuild():
    svc = StatusSummaryService()
    build = AsyncMock(return_value="payload")
    with patch.object(svc, "_build", build):
        await svc.build()
        svc.clear_cache()
        await svc.build()
        assert build.await_count == 2
