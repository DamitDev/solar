"""Tests for per-endpoint model access scoping (S-045)."""

from app.database.endpoints import ApiEndpoint
from app.services.model_access import (
    filter_aliases,
    filter_aliases_for_patterns,
    is_model_allowed,
)


def _endpoint(serve_all_models: bool = True, model_patterns: list[str] | None = None):
    return ApiEndpoint(
        id="ep-1",
        name="test",
        serve_all_models=serve_all_models,
        model_patterns=list(model_patterns or []),
    )


ALIASES = [
    "iris-osl:8b",
    "iris-osl:70b",
    "deepseek-v4-flash:284b",
    "secret-7b",
]


def test_allow_all_short_circuits():
    ep = _endpoint(serve_all_models=True, model_patterns=["iris-*"])
    assert is_model_allowed(ep, "anything-at-all")
    assert filter_aliases(ep, ALIASES) == ALIASES


def test_glob_prefix_match():
    ep = _endpoint(serve_all_models=False, model_patterns=["iris-*"])
    assert is_model_allowed(ep, "iris-osl:8b")
    assert not is_model_allowed(ep, "deepseek-v4-flash:284b")
    assert filter_aliases(ep, ALIASES) == ["iris-osl:8b", "iris-osl:70b"]


def test_glob_star_inside_pattern():
    ep = _endpoint(serve_all_models=False, model_patterns=["*:8b"])
    assert filter_aliases(ep, ALIASES) == ["iris-osl:8b"]


def test_empty_patterns_match_nothing():
    ep = _endpoint(serve_all_models=False, model_patterns=[])
    assert not is_model_allowed(ep, "iris-osl:8b")
    assert filter_aliases(ep, ALIASES) == []


def test_multiple_patterns_union():
    ep = _endpoint(
        serve_all_models=False, model_patterns=["iris-osl:*", "deepseek-v4*"]
    )
    assert filter_aliases(ep, ALIASES) == [
        "iris-osl:8b",
        "iris-osl:70b",
        "deepseek-v4-flash:284b",
    ]


def test_case_sensitive():
    ep = _endpoint(serve_all_models=False, model_patterns=["IRIS-*"])
    assert filter_aliases(ep, ALIASES) == []


def test_filter_aliases_for_patterns_none_is_unrestricted():
    assert filter_aliases_for_patterns(None, ALIASES) == ALIASES


def test_filter_aliases_for_patterns_empty_list_matches_nothing():
    assert filter_aliases_for_patterns([], ALIASES) == []


def test_filter_aliases_for_patterns_keeps_input_order():
    assert filter_aliases_for_patterns(["*"], ALIASES) == ALIASES
