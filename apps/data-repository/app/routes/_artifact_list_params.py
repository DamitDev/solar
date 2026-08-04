"""Shared OpenAPI text for ``GET /api/models`` and ``GET /api/datasets`` list routes."""

ARTIFACT_LIST_SEARCH_DESCRIPTION = (
    "Case-insensitive substring search on artifact ``name``, ``description``, "
    "and the **latest** version's ``metadata`` as JSON text. "
    "SQL wildcards: ``%`` matches any substring, ``_`` matches one character. "
    "Values are passed as bound parameters (no SQL injection). "
    "If the entire string parses as a JSON **object**, rows also match when "
    "latest-version ``metadata`` contains that object (PostgreSQL ``@>``)."
)

ARTIFACT_LIST_PAGE_DESCRIPTION = (
    "1-based page index. When set, ``limit`` and ``offset`` query parameters "
    "are ignored; use ``page_size`` (default 50, max 1000) for page length."
)

ARTIFACT_LIST_PAGE_SIZE_DESCRIPTION = (
    "Results per page when ``page`` is set (default 50, max 1000)."
)
