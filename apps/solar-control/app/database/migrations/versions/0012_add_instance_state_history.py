"""Add instance_state_history table for availability and uptime history

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-21 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.tables"
            "  WHERE table_name = :name"
            "  AND table_schema = current_schema()"
            ")"
        ),
        {"name": table_name},
    )
    return bool(result.scalar())


def _index_exists(conn, index_name: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM pg_indexes"
            "  WHERE indexname = :name"
            "  AND schemaname = current_schema()"
            ")"
        ),
        {"name": index_name},
    )
    return bool(result.scalar())


def upgrade() -> None:
    conn = op.get_bind()
    # The table can already exist (create_all or a partial run), but then its
    # indexes are still missing: probe each index on its own.
    if not _table_exists(conn, "instance_state_history"):
        op.create_table(
            "instance_state_history",
            sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
            sa.Column("alias", sa.Text(), nullable=False),
            sa.Column("instance_id", sa.Text(), nullable=False),
            sa.Column("host_id", sa.Text(), nullable=True),
            sa.Column("state", sa.Text(), nullable=False),
            sa.Column(
                "sampled_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
    if not _index_exists(conn, "idx_state_history_alias_ts"):
        op.create_index(
            "idx_state_history_alias_ts",
            "instance_state_history",
            ["alias", "sampled_at"],
        )
    if not _index_exists(conn, "idx_state_history_instance_ts"):
        op.create_index(
            "idx_state_history_instance_ts",
            "instance_state_history",
            ["instance_id", "sampled_at"],
        )
    if not _index_exists(conn, "idx_state_history_sampled_at"):
        op.create_index(
            "idx_state_history_sampled_at",
            "instance_state_history",
            ["sampled_at"],
        )


def downgrade() -> None:
    op.drop_index("idx_state_history_sampled_at", table_name="instance_state_history")
    op.drop_index("idx_state_history_instance_ts", table_name="instance_state_history")
    op.drop_index("idx_state_history_alias_ts", table_name="instance_state_history")
    op.drop_table("instance_state_history")
