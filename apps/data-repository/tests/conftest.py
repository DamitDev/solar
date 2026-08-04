"""Shared pytest fixtures for data-repository tests."""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.models import RegisterModelVersionRequest


@pytest.fixture
def artifact_id() -> uuid.UUID:
    return uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")


@pytest.fixture
def mock_session() -> AsyncMock:
    """Minimal AsyncSession mock; callers configure return_value per test."""
    session = AsyncMock()
    session.add = MagicMock()
    return session


@pytest.fixture
def basic_request() -> RegisterModelVersionRequest:
    return RegisterModelVersionRequest(harbor_ref="registry.example.com/proj/model:v1")
