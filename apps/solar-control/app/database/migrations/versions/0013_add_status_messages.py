"""Add status_messages table for maintainer status page messages

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-21 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
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
    # index is still missing: probe the index on its own.
    if not _table_exists(conn, "status_messages"):
        op.create_table(
            "status_messages",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=False),
                server_default=sa.func.gen_random_uuid(),
                primary_key=True,
            ),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("type", sa.Text(), nullable=False),
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
    if not _index_exists(conn, "idx_status_messages_window"):
        op.create_index(
            "idx_status_messages_window",
            "status_messages",
            ["start_time", "end_time"],
        )


def downgrade() -> None:
    op.drop_index("idx_status_messages_window", table_name="status_messages")
    op.drop_table("status_messages")
