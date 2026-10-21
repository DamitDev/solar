"""Tests for the instance state history table and StateHistoryDB.

A fake session stands in for Postgres so the statements the helpers build
are pinned without a live database.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.dialects import postgresql

from app.database.state_history import StateHistoryDB, StateSample
from app.database.tables import Base, InstanceStateHistoryRow

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


class _FakeSession:
    def __init__(self, rows=()):
        self.statements: list = []
        self.params: list = []
        self.commits = 0
        self._rows = list(rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, stmt, params=None):
        self.statements.append(stmt)
        self.params.append(params)
        rows = self._rows
        return SimpleNamespace(scalars=lambda: iter(rows))

    async def commit(self):
        self.commits += 1


def _patched(session):
    return patch(
        "app.database.state_history.get_session_factory",
        lambda: lambda: session,
    )


def _sql(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_table_columns_and_indexes():
    table = Base.metadata.tables["instance_state_history"]
    assert {c.name for c in table.columns} == {
        "id",
        "alias",
        "instance_id",
        "host_id",
        "state",
        "sampled_at",
    }
    assert table.c.sampled_at.type.timezone is True

    indexed = {
        tuple(col.name for col in idx.columns): idx.name for idx in table.indexes
    }
    # alias / instance reads over a sampled_at window, and range cleanup.
    assert ("alias", "sampled_at") in indexed
    assert ("instance_id", "sampled_at") in indexed
    assert ("sampled_at",) in indexed


@pytest.mark.asyncio
async def test_insert_batch_writes_all_samples_in_one_statement():
    session = _FakeSession()
    samples = [
        StateSample("core", "i-1", "host-a", "running", NOW),
        StateSample("wind", "i-2", None, "offline", NOW),
    ]
    with _patched(session):
        written = await StateHistoryDB().insert_batch(samples)

    assert written == 2
    assert len(session.statements) == 1
    assert session.commits == 1
    assert session.params[0] == [
        {
            "alias": "core",
            "instance_id": "i-1",
            "host_id": "host-a",
            "state": "running",
            "sampled_at": NOW,
        },
        {
            "alias": "wind",
            "instance_id": "i-2",
            "host_id": None,
            "state": "offline",
            "sampled_at": NOW,
        },
    ]


@pytest.mark.asyncio
async def test_insert_batch_empty_is_a_noop():
    session = _FakeSession()
    with _patched(session):
        assert await StateHistoryDB().insert_batch([]) == 0
    assert session.statements == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_read_window_filters_by_window_and_alias():
    row = InstanceStateHistoryRow(
        id=1,
        alias="core",
        instance_id="i-1",
        host_id="host-a",
        state="running",
        sampled_at=NOW,
    )
    session = _FakeSession([row])
    start = NOW - timedelta(days=90)
    with _patched(session):
        out = await StateHistoryDB().read_window(start, NOW, alias="core")

    assert out == [StateSample("core", "i-1", "host-a", "running", NOW)]
    sql = _sql(session.statements[0])
    assert "sampled_at >=" in sql and "sampled_at <" in sql
    assert "alias = 'core'" in sql
    assert "instance_id =" not in sql
    assert "ORDER BY" in sql


@pytest.mark.asyncio
async def test_read_window_filters_by_instance():
    session = _FakeSession()
    with _patched(session):
        out = await StateHistoryDB().read_window(
            NOW - timedelta(days=1), NOW, instance_id="i-9"
        )

    assert out == []
    sql = _sql(session.statements[0])
    assert "instance_id = 'i-9'" in sql
    assert "alias =" not in sql
