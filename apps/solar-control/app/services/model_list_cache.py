"""In-process TTL cache for the aggregated /v1/models payload.

The payload is a fan-out to every registered backend, so caching it for a few
seconds removes the fan-out from the hot path. The cache is keyed by the key's
model scope (the endpoint's model_patterns tuple, ``None`` when unscoped) and is
per-process; the TTL is short enough that a registry change shows up within a few
seconds. A per-scope lock makes a burst of misses trigger one refresh instead of
one fan-out per request.
"""

import asyncio
import time
from typing import Any

from app.config import settings


class ModelListCache:
    def __init__(self, ttl_s: float) -> None:
        self._ttl_s = ttl_s
        self._entries: dict[tuple[str, ...] | None, tuple[float, dict[str, Any]]] = {}
        self._locks: dict[tuple[str, ...] | None, asyncio.Lock] = {}
        self._generation = 0

    @property
    def generation(self) -> int:
        """Counter bumped by every invalidation."""
        return self._generation

    def get(self, scope: tuple[str, ...] | None) -> dict[str, Any] | None:
        """Cached payload for ``scope``, or None on miss/expiry."""
        entry = self._entries.get(scope)
        if entry is not None and time.monotonic() < entry[0]:
            return entry[1]
        return None

    def set(
        self,
        scope: tuple[str, ...] | None,
        payload: dict[str, Any],
        generation: int | None = None,
    ) -> None:
        """Store ``payload``; a build that raced an invalidation is dropped."""
        if generation is not None and generation != self._generation:
            return
        now = time.monotonic()
        # Drop expired scopes so a long-lived process does not accumulate one
        # entry per endpoint scope it has ever served.
        self._entries = {
            key: entry for key, entry in self._entries.items() if now < entry[0]
        }
        self._entries[scope] = (now + self._ttl_s, payload)

    def lock(self, scope: tuple[str, ...] | None) -> asyncio.Lock:
        """Per-scope single-flight lock."""
        return self._locks.setdefault(scope, asyncio.Lock())

    def invalidate(self) -> None:
        self._entries.clear()
        self._locks.clear()
        self._generation += 1


model_list_cache = ModelListCache(settings.models_cache_ttl_s)
