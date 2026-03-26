"""Initial schema — artifacts and artifact_versions

Revision ID: 0001
Revises:
Create Date: 2026-03-26

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ENUM, JSONB, TIMESTAMP, UUID

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

artifact_category = ENUM(
    "model", "dataset", name="artifact_category", create_type=False
)


def upgrade() -> None:
    artifact_category.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "artifacts",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category", artifact_category, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column(
            "created_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("name", name="uq_artifacts_name"),
    )

    op.create_table(
        "artifact_versions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "artifact_id",
            UUID(as_uuid=True),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("harbor_ref", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.BigInteger),
        sa.Column("digest", sa.String(128)),
        sa.Column(
            "metadata",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "created_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("artifact_id", "version", name="uq_artifact_version"),
    )

    op.create_index("idx_artifacts_category", "artifacts", ["category"])
    op.create_index("idx_artifacts_created_at", "artifacts", ["created_at"])
    op.create_index(
        "idx_artifact_versions_artifact_id", "artifact_versions", ["artifact_id"]
    )
    op.create_index(
        "idx_artifact_versions_created_at", "artifact_versions", ["created_at"]
    )
    op.create_index(
        "idx_artifact_versions_metadata",
        "artifact_versions",
        ["metadata"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("idx_artifact_versions_metadata", table_name="artifact_versions")
    op.drop_index("idx_artifact_versions_created_at", table_name="artifact_versions")
    op.drop_index("idx_artifact_versions_artifact_id", table_name="artifact_versions")
    op.drop_index("idx_artifacts_created_at", table_name="artifacts")
    op.drop_index("idx_artifacts_category", table_name="artifacts")

    op.drop_table("artifact_versions")
    op.drop_table("artifacts")

    artifact_category.drop(op.get_bind(), checkfirst=True)
