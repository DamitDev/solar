"""Add version column to hosts table

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-31 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.columns"
            "  WHERE table_name = 'hosts' AND column_name = 'version'"
            ")"
        )
    )
    if not result.scalar():
        op.add_column("hosts", sa.Column("version", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("hosts", "version")
