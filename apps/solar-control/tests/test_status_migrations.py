"""Tests for the status-page migrations' idempotent index creation.

Both migrations guard their table with an existence probe, but the indexes must be
created even when the table already exists (``create_all`` or a partial run left it
behind without them).
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

VERSIONS = (
    Path(__file__).resolve().parents[1] / "app" / "database" / "migrations" / "versions"
)


def _load(module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name, VERSIONS / f"{module_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeConn:
    """Answers the information_schema and pg_indexes probes from fixed sets."""

    def __init__(self, tables=(), indexes=()):
        self.tables = set(tables)
        self.indexes = set(indexes)

    def execute(self, statement, params=None):
        sql = str(statement)
        name = (params or {}).get("name")
        found = name in (self.indexes if "pg_indexes" in sql else self.tables)
        return SimpleNamespace(scalar=lambda: found)


def _upgrade(module_name: str, conn: _FakeConn):
    """Run the migration's upgrade with a fake connection and collect indexes."""
    created: list[str] = []

    def create_index(name, *args, **kwargs):
        created.append(name)

    module = _load(module_name)
    with (
        patch("alembic.op.get_bind", return_value=conn),
        patch("alembic.op.create_table") as create_table,
        patch("alembic.op.create_index", side_effect=create_index),
    ):
        module.upgrade()
    return create_table, created


def test_state_history_table_exists_still_creates_missing_indexes():
    conn = _FakeConn(tables={"instance_state_history"})

    create_table, created = _upgrade("0012_add_instance_state_history", conn)

    create_table.assert_not_called()
    assert created == [
        "idx_state_history_alias_ts",
        "idx_state_history_instance_ts",
        "idx_state_history_sampled_at",
    ]


def test_state_history_existing_index_is_not_created_again():
    conn = _FakeConn(
        tables={"instance_state_history"},
        indexes={"idx_state_history_alias_ts"},
    )

    _, created = _upgrade("0012_add_instance_state_history", conn)

    assert created == [
        "idx_state_history_instance_ts",
        "idx_state_history_sampled_at",
    ]


def test_status_messages_table_exists_still_creates_missing_index():
    conn = _FakeConn(tables={"status_messages"})

    create_table, created = _upgrade("0013_add_status_messages", conn)

    create_table.assert_not_called()
    assert created == ["idx_status_messages_window"]


def test_status_messages_existing_index_is_not_created_again():
    conn = _FakeConn(
        tables={"status_messages"},
        indexes={"idx_status_messages_window"},
    )

    _, created = _upgrade("0013_add_status_messages", conn)

    assert created == []
