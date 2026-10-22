"""Tests for the transactional status message update.

The window validation lives in the same transaction as the write: the row is read
under a lock, the merged window is checked, and only then is anything changed.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy.dialects import postgresql

from app.database.status_messages import StatusMessageDB, StatusMessageWindowError
from app.database.tables import StatusMessageRow
from app.models.status_message import StatusMessageUpdate

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
MESSAGE_ID = "00000000-0000-0000-0000-000000000001"


class _FakeSession:
    def __init__(self, row):
        self.row = row
        self.statements: list = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(scalar_one_or_none=lambda: self.row)

    async def commit(self):
        self.commits += 1

    async def refresh(self, row):
        pass


def _row(**overrides) -> StatusMessageRow:
    data = {
        "id": MESSAGE_ID,
        "body": "Weekend maintenance",
        "type": "planned maintenance",
        "start_time": NOW,
        "end_time": NOW + timedelta(hours=4),
        "created_at": NOW,
    }
    data.update(overrides)
    return StatusMessageRow(**data)


def _patched(session):
    return patch(
        "app.database.status_messages.get_session_factory",
        lambda: lambda: session,
    )


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


@pytest.mark.asyncio
async def test_update_rejects_inverted_merged_window_and_writes_nothing():
    row = _row()
    session = _FakeSession(row)
    data = StatusMessageUpdate(end_time=NOW - timedelta(hours=1))

    with _patched(session), pytest.raises(StatusMessageWindowError) as exc:
        await StatusMessageDB().update(MESSAGE_ID, data)

    assert str(exc.value) == "start_time must not be after end_time"
    # The rejected update wrote nothing: no commit and the stored row intact.
    assert session.commits == 0
    assert row.body == "Weekend maintenance"
    assert row.end_time == NOW + timedelta(hours=4)
    # The row was read under a lock, so no concurrent update could slip
    # between the merge and the write.
    assert "FOR UPDATE" in _sql(session.statements[0])


@pytest.mark.asyncio
async def test_locked_read_keeps_a_valid_partial_update_working():
    row = _row()
    session = _FakeSession(row)
    data = StatusMessageUpdate(body="Maintenance extended")

    with _patched(session):
        updated = await StatusMessageDB().update(MESSAGE_ID, data)

    assert updated is not None
    assert updated.body == "Maintenance extended"
    # The untouched half of the window is carried over from the locked read.
    assert updated.start_time == NOW
    assert updated.end_time == NOW + timedelta(hours=4)
    assert session.commits == 1
    assert "FOR UPDATE" in _sql(session.statements[0])
