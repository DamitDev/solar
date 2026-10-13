"""Unit tests for app.database.pool_metrics (#99 pool observability).

No DB needed: a tiny real SQLAlchemy QueuePool with a dummy DBAPI creator
fires the real pool events. Fast — every test runs in well under a second.
"""

import logging
import warnings

import pytest
from sqlalchemy.exc import TimeoutError as SaTimeoutError
from sqlalchemy.pool import QueuePool

from app.database.pool_metrics import (
    install_pool_metrics,
    render_metrics,
    reset_counters,
    snapshot_counters,
)


class _DummyConn:
    """Minimal DBAPI-shaped connection for QueuePool's creator."""

    def rollback(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


def _make_pool() -> QueuePool:
    return QueuePool(_DummyConn, pool_size=1, max_overflow=0, timeout=0.1)


@pytest.fixture
def _clean_metrics():
    import app.database.pool_metrics as pm

    pm._POOL = None
    reset_counters()
    yield
    reset_counters()


def test_checkout_checkin_counters(_clean_metrics):
    pool = _make_pool()
    install_pool_metrics(pool)
    conn = pool.connect()
    assert snapshot_counters()["checkout"] == 1
    assert snapshot_counters()["checkin"] == 0
    conn.close()
    assert snapshot_counters()["checkout"] == 1
    assert snapshot_counters()["checkin"] == 1


def test_gauges_read_at_scrape(_clean_metrics):
    pool = _make_pool()
    install_pool_metrics(pool)
    text = render_metrics()
    assert "solar_db_pool_size" in text
    assert "solar_db_pool_checkedout" in text
    assert "solar_db_pool_overflow" in text
    assert "solar_db_pool_checkedin" in text
    # While a connection is held, checkedout must read 1.
    conn = pool.connect()
    assert "solar_db_pool_checkedout 1" in render_metrics()
    conn.close()
    assert "solar_db_pool_checkedout 0" in render_metrics()


def test_prometheus_text_format_shape(_clean_metrics):
    pool = _make_pool()
    install_pool_metrics(pool)
    lines = render_metrics().splitlines()
    counters = [ln for ln in lines if ln.startswith("solar_db_pool_checkout_total ")]
    assert len(counters) == 1
    # One HELP + one TYPE comment per metric line triplet.
    assert lines[0].startswith("# HELP solar_db_pool_checkout_total")
    assert lines[1].startswith("# TYPE solar_db_pool_checkout_total counter")
    # Version marker is part of the media type, not the body — but the body
    # must be plain text parseable as key/value pairs.
    for ln in lines:
        if not ln.startswith("#"):
            assert " " in ln


def test_timeout_counter_fires_on_checkout_timeout(_clean_metrics):
    pool = _make_pool()
    install_pool_metrics(pool)
    held = pool.connect()
    assert snapshot_counters()["checkout_timeout"] == 0
    with pytest.raises(SaTimeoutError):
        pool.connect()
    assert snapshot_counters()["checkout_timeout"] == 1
    # The checkout itself must NOT count as a successful checkout event.
    assert snapshot_counters()["checkout"] == 1
    held.close()


def test_invalidate_and_close_detached_counters(_clean_metrics):
    pool = _make_pool()
    install_pool_metrics(pool)
    conn = pool.connect()
    conn.invalidate()
    assert snapshot_counters()["invalidate"] == 1
    assert snapshot_counters()["soft_invalidate"] == 0
    # invalidate() implies a check-in (the record is returned to the pool
    # in an invalidated state).
    assert snapshot_counters()["checkin"] == 1


def test_soft_invalidate_counter(_clean_metrics):
    pool = _make_pool()
    install_pool_metrics(pool)
    conn = pool.connect()
    conn.invalidate(soft=True)
    assert snapshot_counters()["soft_invalidate"] == 1
    assert snapshot_counters()["invalidate"] == 0
    conn.close()


def test_install_is_idempotent(_clean_metrics):
    pool = _make_pool()
    install_pool_metrics(pool)
    install_pool_metrics(pool)  # second call must not double-count
    conn = pool.connect()
    conn.close()
    assert snapshot_counters()["checkout"] == 1
    assert snapshot_counters()["checkin"] == 1
    # _do_get must be wrapped exactly once, not nested.
    assert pool._solar_metrics_installed is True


def test_terminate_log_signature_counter(_clean_metrics):
    pool = _make_pool()
    install_pool_metrics(pool)
    pool_logger = logging.getLogger("sqlalchemy.pool")
    before = len(pool_logger.handlers)
    install_pool_metrics(pool)
    # A second install must not attach another handler.
    assert len(pool_logger.handlers) == before
    # Emit a record carrying the #99 signature; the handler must count it.
    record = logging.LogRecord(
        "sqlalchemy.pool",
        logging.ERROR,
        __file__,
        1,
        "Exception terminating connection <object>",
        None,
        None,
    )
    for handler in pool_logger.handlers:
        if isinstance(handler, logging.Handler):
            handler.emit(record)
    assert snapshot_counters()["terminate"] == 1


def test_gc_sa_warning_counter(_clean_metrics):
    import app.database.pool_metrics as pm

    pool = _make_pool()
    install_pool_metrics(pool)
    # pytest's per-test warning capture replaces warnings.showwarning, so
    # re-install the module hook to guarantee it is the active showwarning
    # for the warn below (in production nothing replaces it after install).
    pm._WARNINGS_HOOK_INSTALLED = False
    pm._install_warnings_hook()
    warnings.warn(
        "garbage collector is trying to clean up non-checked-in connection <obj>",
        UserWarning,
    )
    assert snapshot_counters()["gc_non_checked_in"] == 1
    # A second warn must also count (the hook chains to the previous
    # showwarning, so the warning still reaches pytest's capture).
    warnings.warn(
        "garbage collector is trying to clean up non-checked-in connection <obj>",
        UserWarning,
    )
    assert snapshot_counters()["gc_non_checked_in"] == 2
    # Restore pytest's capture as the active showwarning.
    warnings.showwarning = None


def test_gauges_render_zero_without_install():
    import app.database.pool_metrics as pm

    pm._POOL = None
    reset_counters()
    text = render_metrics()
    assert "solar_db_pool_checkedout 0" in text
    # Counters render as 0 without any install.
    assert "solar_db_pool_checkout_total 0" in text
    assert "solar_db_pool_size 0" in text
