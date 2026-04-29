"""Unit tests for :mod:`app.pagination`."""

from app.pagination import resolve_list_pagination


def test_offset_mode_unchanged():
    assert resolve_list_pagination(limit=25, offset=3, page=None, page_size=None) == (
        25,
        3,
    )


def test_page_mode_overrides_limit_offset():
    assert resolve_list_pagination(limit=99, offset=99, page=2, page_size=10) == (
        10,
        10,
    )


def test_page_without_page_size_defaults_to_50():
    assert resolve_list_pagination(limit=10, offset=5, page=3, page_size=None) == (
        50,
        100,
    )


def test_page_size_capped_at_1000():
    assert resolve_list_pagination(limit=50, offset=0, page=1, page_size=5000) == (
        1000,
        0,
    )
