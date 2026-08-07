"""Age of a cached Redis entry.

The WS-first read model (C4/C5) stores every host-pushed payload as
``{"at": <iso8601>, ...}`` and decides staleness on read rather than with a
TTL, so both the reconciler and the management routes need the same age
calculation. It lives here because it belongs to neither.
"""

from datetime import datetime, timezone
from typing import Any


def entry_age_s(at: Any) -> float | None:
    """Age in seconds of an ISO-8601 ``at`` stamp, or None if unusable.

    ``at`` is written by control (see ``host_pull_progress`` and
    ``set_host_resource_snapshot``), never by the host, so clock skew across
    the fleet cannot make an entry look fresh forever. An entry persisted by
    an older build may still be naive, and subtracting a naive from an aware
    datetime raises ``TypeError``, so the naive case is assumed UTC rather
    than allowed to propagate out of a freshness check.
    """
    if not isinstance(at, str):
        return None
    try:
        at_dt = datetime.fromisoformat(at)
        if at_dt.tzinfo is None:
            at_dt = at_dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - at_dt).total_seconds()
    except (ValueError, TypeError):
        return None
