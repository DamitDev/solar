"""Add cached_tokens to gateway_requests

The prompt-cache hit portion of input tokens for cache-aware backends
(llama.cpp and SGLang). ``NULL`` means "backend is not cache-aware"
(HuggingFace) and must not be conflated with a real 0 -- aggregations
coalesce NULL to 0 for sums, but the cache-hit rate only counts rows where
cached_tokens IS NOT NULL. No backfill: historical rows stay unknown.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-17 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    result = conn.execute(
        sa.text(
            "SELECT EXISTS ("
            "  SELECT 1 FROM information_schema.columns"
            "  WHERE table_name = 'gateway_requests'"
            "    AND column_name = 'cached_tokens'"
            ")"
        )
    )
    if not result.scalar():
        op.add_column(
            "gateway_requests",
            sa.Column("cached_tokens", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    op.drop_column("gateway_requests", "cached_tokens")
