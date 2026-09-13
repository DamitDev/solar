"""In-process TTL cache for virtual model definitions (S-060).

Virtual models are low-churn configuration read on every gateway request
and /v1/models call; a short in-process TTL avoids a DB round-trip per
request without needing Redis fan-out. Every write path invalidates.
"""

import time

from app.models.virtual_model import VirtualModelResponse

TTL_S = 5.0


class VirtualModelCache:
    def __init__(self, ttl_s: float = TTL_S) -> None:
        self._ttl_s = ttl_s
        self._entries: list[VirtualModelResponse] | None = None
        self._expires_at = 0.0

    def get_all(self) -> list[VirtualModelResponse] | None:
        """Cached list, or None on miss/expiry."""
        if self._entries is not None and time.monotonic() < self._expires_at:
            return self._entries
        return None

    def set_all(self, entries: list[VirtualModelResponse]) -> None:
        self._entries = entries
        self._expires_at = time.monotonic() + self._ttl_s

    def invalidate(self) -> None:
        self._entries = None
        self._expires_at = 0.0


virtual_model_cache = VirtualModelCache()
