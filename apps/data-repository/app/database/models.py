"""SQLAlchemy ORM models for Data Repository metadata.

These models define the schema for Alembic migration generation.
Runtime queries use asyncpg directly via the connection pool in connection.py.
"""

from sqlalchemy import (
    BigInteger,
    Column,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


artifact_category = Enum("model", "dataset", name="artifact_category")


class Artifact(Base):
    __tablename__ = "artifacts"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    name = Column(String(255), nullable=False)
    category = Column(artifact_category, nullable=False)
    description = Column(Text)
    created_at = Column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at = Column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    versions = relationship(
        "ArtifactVersion", back_populates="artifact", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("name", name="uq_artifacts_name"),
        Index("idx_artifacts_category", "category"),
        Index("idx_artifacts_created_at", "created_at"),
    )


class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    artifact_id = Column(
        UUID(as_uuid=True),
        ForeignKey("artifacts.id", ondelete="CASCADE"),
        nullable=False,
    )
    version = Column(String(128), nullable=False)
    harbor_ref = Column(String(512), nullable=False)
    size_bytes = Column(BigInteger)
    digest = Column(String(128))
    metadata_ = Column("metadata", JSONB, nullable=False, server_default=text("'{}'"))
    created_at = Column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    artifact = relationship("Artifact", back_populates="versions")

    __table_args__ = (
        UniqueConstraint("artifact_id", "version", name="uq_artifact_version"),
        Index("idx_artifact_versions_artifact_id", "artifact_id"),
        Index("idx_artifact_versions_created_at", "created_at"),
        Index(
            "idx_artifact_versions_metadata",
            "metadata",
            postgresql_using="gin",
        ),
    )
