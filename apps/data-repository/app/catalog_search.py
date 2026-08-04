"""Normalization and documentation for catalog list ``search`` parameters.

Search strings are passed to PostgreSQL ``ILIKE`` (name, description, and the
latest version's JSON metadata as text). Values are always bound as query
parameters — never concatenated into SQL — so arbitrary characters cannot
alter query structure (no SQL injection).

``%`` and ``_`` are SQL wildcard characters: ``%`` matches any substring and
``_`` matches a single character. Callers may use them intentionally; literal
``%`` or ``_`` in a phrase is not supported in this API.
"""

from __future__ import annotations

MAX_ARTIFACT_LIST_SEARCH_LEN = 500


def normalize_artifact_list_search(raw: str | None) -> str | None:
    """Return stripped search text, or ``None`` if absent or whitespace-only.

    Strings longer than :data:`MAX_ARTIFACT_LIST_SEARCH_LEN` are truncated to
    limit pathological patterns and payload size.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    if len(s) > MAX_ARTIFACT_LIST_SEARCH_LEN:
        return s[:MAX_ARTIFACT_LIST_SEARCH_LEN]
    return s


def ilike_substring_pattern(normalized: str) -> str:
    """Build the ``ILIKE`` pattern for substring search (wildcards preserved)."""
    return f"%{normalized}%"
