"""Add pg_trgm GIN indexes for artifact list search (name, description).

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-29

"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    op.create_index(
        "idx_artifacts_name_trgm",
        "artifacts",
        ["name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name": "gin_trgm_ops"},
    )
    op.create_index(
        "idx_artifacts_description_trgm",
        "artifacts",
        ["description"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"description": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index("idx_artifacts_description_trgm", table_name="artifacts")
    op.drop_index("idx_artifacts_name_trgm", table_name="artifacts")
