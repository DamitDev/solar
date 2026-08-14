"""Add supported_backends column to hosts table (SGLang backend)

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-14 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
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
    # NULL rather than an empty list: a host that has not registered since the
    # upgrade reports nothing, and "no opinion" must not read as "supports
    # nothing" in the placement filter.
    if not _has_column(conn, "supported_backends"):
        op.add_column(
            "hosts",
            sa.Column("supported_backends", postgresql.JSONB(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("hosts", "supported_backends")
