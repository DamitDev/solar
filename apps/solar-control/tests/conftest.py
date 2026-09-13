"""Shared pytest fixtures for the solar-control test suite."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _seed_empty_virtual_model_cache():
    """Default the virtual-model cache to an empty list for every test.

    Gateway tests exercise route_request / stream_request / models listing
    without a database; on a cache miss the virtual pre-resolution would
    otherwise hit VirtualModelDB and blow up with "Database not initialized".
    Tests that need real virtual models patch or re-seed the cache explicitly.
    """
    from app.services.virtual_model_cache import virtual_model_cache

    virtual_model_cache.set_all([])
    yield
    virtual_model_cache.invalidate()


@pytest.fixture
def repo_settings():
    """Patch ``app.model_resolvers.repo.settings`` with sensible defaults.

    Yields the mock so tests can override individual fields (e.g. unset
    ``data_repository_url`` to exercise the unconfigured-config path).
    """
    with patch("app.model_resolvers.repo.settings") as mock_settings:
        mock_settings.data_repository_url = "http://data-repo:8000"
        mock_settings.data_repository_api_key = ""
        mock_settings.data_repository_timeout_s = 10.0
        yield mock_settings
