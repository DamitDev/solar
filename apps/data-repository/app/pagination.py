"""Shared pagination helpers for list endpoints."""

from __future__ import annotations


def resolve_list_pagination(
    *,
    limit: int,
    offset: int,
    page: int | None,
    page_size: int | None,
) -> tuple[int, int]:
    """Return ``(limit, offset)`` for repository queries.

    When ``page`` is set, **page-based** pagination is used and ``limit`` /
    ``offset`` from the query string are ignored. ``page_size`` defaults to
    ``50`` when omitted. When ``page`` is omitted, ``limit`` and ``offset`` are
    used unchanged (typically validated by FastAPI ``Query`` constraints).
    """
    if page is not None:
        ps = page_size if page_size is not None else 50
        lim = min(max(ps, 1), 1000)
        off = (page - 1) * lim
        return lim, off
    return limit, offset
