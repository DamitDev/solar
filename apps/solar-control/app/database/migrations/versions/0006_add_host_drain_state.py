"""Add drain state columns to hosts table (S-043)

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(conn, column: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.columns"
            "  WHERE table_name = 'hosts' AND column_name = :column"
            ")"
        ),
        {"column": column},
    )
    return bool(result.scalar())


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn, "drain_state"):
        op.add_column("hosts", sa.Column("drain_state", sa.Text(), nullable=True))
    if not _has_column(conn, "drain_requested_at"):
        op.add_column(
            "hosts",
            sa.Column("drain_requested_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("hosts", "drain_requested_at")
    op.drop_column("hosts", "drain_state")
