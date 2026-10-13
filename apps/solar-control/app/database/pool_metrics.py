"""Prometheus pool-health metrics for solar-control (#99).

Stdlib-only — no ``prometheus_client`` dependency; the text format (v0.0.4)
is rendered by hand. Counters are wired to REAL SQLAlchemy pool events
(checkout, checkin, invalidate, soft_invalidate, close_detached) via
``sqlalchemy.event.listen`` on the engine's pool, and to the production
signatures from issue #99 — the ``Exception terminating connection`` log
record and the ``non-checked-in connection`` SAWarning — via a logging
handler on the ``sqlalchemy.pool`` logger and a ``warnings.showwarning``
hook. Checkout timeouts are counted by wrapping the pool's ``_do_get``
once at install: there is NO 'timeout' pool event in SQLAlchemy, and
attempting to listen for one raises ``InvalidRequestError`` at startup.
Gauges (pool size / checkedout / overflow / checkedin) are read at scrape
time.

``install_pool_metrics`` is idempotent per pool: calling it twice must not
double-count. Counters are process-global (one engine per process in
production); simple int ops under the GIL keep them thread/asyncio-safe.
"""

import logging
import warnings
from typing import Any

from sqlalchemy.event import listen
from sqlalchemy.exc import TimeoutError

# Log signature from issue #99: a level-triggered anyio cancel scope can
# strand a connection record until GC; SQLAlchemy logs this on the
# ``sqlalchemy.pool`` logger when the pool drains.
_TERMINATE_SIGNATURE = "Exception terminating connection"
# SAWarning signature from issue #99: the garbage collector cleaning up a
# non-checked-in connection.
_GC_SIGNATURE = "non-checked-in connection"

# Pool events counted. There is deliberately no 'timeout' event — it does
# not exist in SQLAlchemy.
_POOL_EVENTS = (
    "checkout",
    "checkin",
    "invalidate",
    "soft_invalidate",
    "close_detached",
)


class _Counter:
    """A single integer counter; int ops are atomic enough under the GIL."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0

    def bump(self) -> None:
        self.value += 1

    def reset(self) -> None:
        self.value = 0


# Process-global counters, insertion-ordered so render_metrics output is
# stable across scrapes.
_CHECKOUT = _Counter()
_CHECKIN = _Counter()
_INVALIDATE = _Counter()
_SOFT_INVALIDATE = _Counter()
_CLOSE_DETACHED = _Counter()
_CHECKOUT_TIMEOUT = _Counter()
_TERMINATE = _Counter()
_GC = _Counter()

_COUNTERS: dict[str, _Counter] = {
    "checkout": _CHECKOUT,
    "checkin": _CHECKIN,
    "invalidate": _INVALIDATE,
    "soft_invalidate": _SOFT_INVALIDATE,
    "close_detached": _CLOSE_DETACHED,
    "checkout_timeout": _CHECKOUT_TIMEOUT,
    "terminate": _TERMINATE,
    "gc_non_checked_in": _GC,
}

_COUNTER_HELP = {
    "checkout": "Pool checkouts from SQLAlchemy pool events.",
    "checkin": "Pool check-ins from SQLAlchemy pool events.",
    "invalidate": "Hard invalidations from SQLAlchemy pool events.",
    "soft_invalidate": "Soft invalidations from SQLAlchemy pool events.",
    "close_detached": "Detached-connection closes from SQLAlchemy pool events.",
    "checkout_timeout": "Checkout timeouts (sqlalchemy.exc.TimeoutError).",
    "terminate": "Logged 'Exception terminating connection' records (#99).",
    "gc_non_checked_in": "GC 'non-checked-in connection' SAWarnings (#99).",
}

_GAUGE_HELP = {
    "solar_db_pool_size": "Configured pool size.",
    "solar_db_pool_checkedout": "Connections currently checked out.",
    "solar_db_pool_overflow": "Current overflow count.",
    "solar_db_pool_checkedin": "Connections currently checked in (idle in pool).",
}

# The pool whose gauges render_metrics reads. Set at install; None means
# no engine has been installed yet and all gauges read 0.
_POOL: Any | None = None

_WARNINGS_HOOK_INSTALLED = False


class _PoolLogCounter(logging.Handler):
    """Count ``Exception terminating connection`` records on the pool logger.

    One instance per process (install_pool_metrics refuses to add a second
    to the same logger), so ``self.count`` is process-global.
    """

    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if _TERMINATE_SIGNATURE in record.getMessage():
            self.count += 1
            _TERMINATE.bump()


def _counter_listener(name: str):
    """Build an event-listener callback bumping the named counter."""

    def _listener(*_args) -> None:
        _COUNTERS[name].bump()

    return _listener


def _install_warnings_hook() -> None:
    """Count the ``non-checked-in connection`` SAWarning process-globally.

    Chains to the previous ``warnings.showwarning`` so existing warning
    behavior is preserved. Runs once per process; the installed-flag check
    in ``install_pool_metrics`` keeps a second install from double-chaining.
    """
    global _WARNINGS_HOOK_INSTALLED
    previous = warnings.showwarning

    def _hook(message, category, filename, lineno, file=None, line=None):
        if _GC_SIGNATURE in str(message):
            _GC.bump()
        if previous is not None:
            previous(message, category, filename, lineno, file=file, line=line)

    warnings.showwarning = _hook
    _WARNINGS_HOOK_INSTALLED = True


def _resolve_pool(engine: Any) -> Any:
    """Return the SQLAlchemy Pool for an Engine, AsyncEngine, or Pool."""
    if hasattr(engine, "sync_engine"):
        return engine.sync_engine.pool
    if hasattr(engine, "checkedout"):
        return engine
    raise TypeError(
        f"install_pool_metrics expects an Engine/AsyncEngine/Pool, "
        f"got {type(engine).__name__}"
    )


def install_pool_metrics(engine: Any) -> None:
    """Wire pool event counters + production-signature hooks onto *engine*.

    Idempotent per pool: a second call on the same pool (or engine) must
    not double-count. Listens for the five REAL pool events, wraps the
    pool's ``_do_get`` once to count checkout timeouts, attaches the
    ``sqlalchemy.pool`` logging handler, and installs the
    ``warnings.showwarning`` hook.
    """
    pool = _resolve_pool(engine)
    if getattr(pool, "_solar_metrics_installed", False):
        return
    pool._solar_metrics_installed = True  # type: ignore[attr-defined]
    global _POOL
    _POOL = pool

    for event_name in _POOL_EVENTS:
        listen(pool, event_name, _counter_listener(event_name))

    # Checkout timeouts: wrap the pool's _do_get once. There is no 'timeout'
    # pool event in SQLAlchemy — listening for one raises at startup. The
    # wrap works for both sync and async pools: _ConnectionRecord.checkout
    # calls _do_get synchronously in both (the asyncio adapter lives in the
    # queue), so a TimeoutError from either path is counted here.
    original_do_get = pool._do_get

    def _do_get_counting_timeout():
        try:
            return original_do_get()
        except TimeoutError:
            _CHECKOUT_TIMEOUT.bump()
            raise

    pool._do_get = _do_get_counting_timeout

    # Production log signature counter: a logging.Handler on the
    # 'sqlalchemy.pool' logger counting 'Exception terminating connection'.
    pool_logger = logging.getLogger("sqlalchemy.pool")
    if not any(isinstance(h, _PoolLogCounter) for h in pool_logger.handlers):
        pool_logger.addHandler(_PoolLogCounter())

    # Production SAWarning counter: warnings.showwarning override, installed
    # once (process-global) so a second install cannot chain it twice.
    if not _WARNINGS_HOOK_INSTALLED:
        _install_warnings_hook()


def snapshot_counters() -> dict[str, int]:
    """Return the current process-global counter values (test/debug hook)."""
    return {name: counter.value for name, counter in _COUNTERS.items()}


def reset_counters() -> None:
    """Zero the process-global counters (test hook; installs are unaffected)."""
    for counter in _COUNTERS.values():
        counter.reset()


def render_metrics() -> str:
    """Render the current metrics in Prometheus text format (v0.0.4)."""
    lines: list[str] = []
    for name, counter in _COUNTERS.items():
        metric = f"solar_db_pool_{name}_total"
        lines.append(f"# HELP {metric} {_COUNTER_HELP[name]}")
        lines.append(f"# TYPE {metric} counter")
        lines.append(f"{metric} {counter.value}")
    pool = _POOL
    gauges = (
        ("solar_db_pool_size", pool.size() if pool is not None else 0),
        ("solar_db_pool_checkedout", pool.checkedout() if pool is not None else 0),
        ("solar_db_pool_overflow", pool.overflow() if pool is not None else 0),
        ("solar_db_pool_checkedin", pool.checkedin() if pool is not None else 0),
    )
    for name, value in gauges:
        lines.append(f"# HELP {name} {_GAUGE_HELP[name]}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"
