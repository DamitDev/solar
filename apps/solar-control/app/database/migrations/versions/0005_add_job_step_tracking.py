"""Add current_step_name and current_step_index to jobs table

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "current_step_name",
            sa.Text(),
            nullable=True,
            comment="Name of the currently executing pipeline step (e.g. 'train')",
        ),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "current_step_index",
            sa.Integer(),
            nullable=True,
            comment="Zero-based index of the currently executing pipeline step",
        ),
    )


def downgrade() -> None:
    op.drop_column("jobs", "current_step_index")
    op.drop_column("jobs", "current_step_name")
