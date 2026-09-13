"""Add virtual_models table for stable model aliases (S-060)

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(conn, table_name: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.tables"
            "  WHERE table_name = :name"
            ")"
        ),
        {"name": table_name},
    )
    return result.scalar()


def upgrade() -> None:
    conn = op.get_bind()

    if not _table_exists(conn, "virtual_models"):
        op.create_table(
            "virtual_models",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=False),
                server_default=sa.func.gen_random_uuid(),
                primary_key=True,
            ),
            sa.Column("name", sa.Text(), nullable=False, unique=True),
            sa.Column("targets", postgresql.JSONB(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column(
                "contract",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

        op.create_index(
            "idx_virtual_models_name", "virtual_models", ["name"], unique=True
        )


def downgrade() -> None:
    op.drop_index("idx_virtual_models_name", table_name="virtual_models")
    op.drop_table("virtual_models")
