"""Unit tests for :mod:`app.catalog_search`."""

from app.catalog_search import (
    MAX_ARTIFACT_LIST_SEARCH_LEN,
    ilike_substring_pattern,
    normalize_artifact_list_search,
)


def test_normalize_none_returns_none():
    assert normalize_artifact_list_search(None) is None


def test_normalize_blank_and_whitespace_returns_none():
    assert normalize_artifact_list_search("") is None
    assert normalize_artifact_list_search("   \t  ") is None


def test_normalize_strips_edges():
    assert normalize_artifact_list_search("  iris  ") == "iris"


def test_normalize_truncates_long_input():
    raw = "x" * (MAX_ARTIFACT_LIST_SEARCH_LEN + 50)
    out = normalize_artifact_list_search(raw)
    assert len(out) == MAX_ARTIFACT_LIST_SEARCH_LEN


def test_ilike_substring_pattern_wraps_percent():
    assert ilike_substring_pattern("iris") == "%iris%"
