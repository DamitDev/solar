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


@pytest.fixture(autouse=True)
def _clear_model_list_cache():
    """Default the models-list cache to empty for every test.

    The payload cache is module-level and per-process; without a clear a test
    could observe a previous test's payload and skip its own fan-out.
    """
    from app.services.model_list_cache import model_list_cache

    model_list_cache.invalidate()
    yield
    model_list_cache.invalidate()


@pytest.fixture(autouse=True)
def _clear_status_summary_cache():
    """Default the status response cache to empty for every test.

    The response cache is module-level and per-process; without a clear a test
    could observe a previous test's payload and skip its own build.
    """
    from app.services.status_summary import status_summary_service

    status_summary_service.clear_cache()
    yield
    status_summary_service.clear_cache()


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
