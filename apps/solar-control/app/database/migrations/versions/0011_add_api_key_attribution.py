"""Add API key attribution to gateway_requests

Per-request attribution to the named /v1 credential. ``api_key_id`` is the
UUID FK to ``api_keys.id`` (ON DELETE SET NULL, mirroring endpoint_id) and
``api_key_name`` is a snapshot of the key's name at log time, so history
keeps its attribution label after the key is deleted or renamed. No
backfill: pre-migration rows and management-key traffic stay anonymous.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(conn, name: str) -> bool:
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.columns"
            "  WHERE table_name = 'gateway_requests'"
            "    AND column_name = :name"
            ")"
        ),
        {"name": name},
    )
    return bool(result.scalar())


def upgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, "api_key_id"):
        op.add_column(
            "gateway_requests",
            sa.Column(
                "api_key_id",
                sa.dialects.postgresql.UUID(as_uuid=False),
                sa.ForeignKey("api_keys.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    if not _column_exists(conn, "api_key_name"):
        op.add_column(
            "gateway_requests",
            sa.Column("api_key_name", sa.Text(), nullable=True),
        )
    result = conn.execute(
        sa.text("SELECT 1 FROM pg_indexes" " WHERE indexname = 'idx_requests_api_key'")
    )
    if not result.scalar():
        op.create_index("idx_requests_api_key", "gateway_requests", ["api_key_id"])


def downgrade() -> None:
    op.drop_index("idx_requests_api_key", table_name="gateway_requests")
    op.drop_column("gateway_requests", "api_key_name")
    op.drop_column("gateway_requests", "api_key_id")
