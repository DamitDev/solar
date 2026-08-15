"""Per-endpoint model alias scoping.

The single source of truth for how an endpoint's ``model_patterns`` match
against registry aliases. Semantics are fnmatch globs over the alias string
(e.g. ``iris-osl:*``), applied case-sensitively. ``serve_all_models``
short-circuits to allow-all so a scoped endpoint can never accidentally
"include everything" through an empty pattern list.

The gateway threads the raw ``model_patterns`` list through routing internally
(``filter_aliases_for_patterns``, where ``None`` means unrestricted) so the
context is carried without needing a DB round-trip per request; endpoint
shaped filtering (``is_model_allowed`` / ``filter_aliases``) is used by the
management routes and tests.
"""

from collections.abc import Iterable
from fnmatch import fnmatchcase
from typing import Any


def _matches_any(alias: str, patterns: list[str]) -> bool:
    """True when the alias matches at least one glob pattern."""
    return any(fnmatchcase(alias, pattern) for pattern in patterns)


def is_model_allowed(endpoint: Any, alias: str) -> bool:
    """Whether a single alias is visible/served for an endpoint."""
    if endpoint.serve_all_models:
        return True
    return _matches_any(alias, list(endpoint.model_patterns or []))


def filter_aliases(endpoint: Any, aliases: Iterable[str]) -> list[str]:
    """Registry aliases an endpoint may use, in input order."""
    if endpoint.serve_all_models:
        return list(aliases)
    patterns = list(endpoint.model_patterns or [])
    return [alias for alias in aliases if _matches_any(alias, patterns)]


def filter_aliases_for_patterns(
    patterns: list[str] | None, aliases: Iterable[str]
) -> list[str]:
    """Filter aliases by raw glob patterns.

    Used by the gateway where ``None`` means unrestricted (management key /
    untouched request) and a list is an endpoint's explicit scope: an empty
    list therefore matches nothing, never everything.
    """
    if patterns is None:
        return list(aliases)
    return [alias for alias in aliases if _matches_any(alias, patterns)]
