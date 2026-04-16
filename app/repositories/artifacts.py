"""SQL access layer for artifact and artifact_version rows.

The canonical interface is :class:`ArtifactRepository`, constructed with an
:class:`~sqlalchemy.ext.asyncio.AsyncSession` whose transaction the caller
owns.

No Harbor imports, no HTTPException references, no business logic beyond
executing SQL.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Artifact, ArtifactVersion
from app.exceptions import (
    ArtifactCategoryConflictError,
    DatasetNotFoundError,
    DatasetVersionNotFoundError,
    ModelNotFoundError,
    ModelVersionNotFoundError,
    VersionAlreadyExistsError,
)
from app.types import ArtifactCategory


@dataclass(frozen=True)
class ArtifactVersionRecord:
    name: str
    version: str
    category: str
    harbor_ref: str
    size_bytes: int | None
    checksum: str | None
    created_at: datetime
    metadata: dict[str, Any]


class ArtifactRepository:
    """Persistence operations for artifacts and their versions.

    Construct with an :class:`~sqlalchemy.ext.asyncio.AsyncSession` whose
    transaction boundary is owned by the caller (e.g. the
    ``get_db_session`` FastAPI dependency).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_or_fetch_artifact(
        self,
        name: str,
        category: ArtifactCategory = "model",
    ) -> uuid.UUID:
        """Insert an artifact if it does not exist, or fetch the existing row.

        Returns the artifact ``id`` when the row has the requested category.
        Raises :exc:`~app.exceptions.ArtifactCategoryConflictError` when the
        row already exists with a different category.
        """
        insert_stmt = (
            pg_insert(Artifact)
            .values(name=name, category=category)
            .on_conflict_do_nothing(index_elements=["name"])
            .returning(Artifact.id, Artifact.category)
        )
        result = await self._session.execute(insert_stmt)
        row = result.fetchone()

        if row is None:
            select_stmt = select(Artifact.id, Artifact.category).where(
                Artifact.name == name
            )
            result = await self._session.execute(select_stmt)
            row = result.fetchone()

        if row is None:
            raise RuntimeError(f"Could not find or create artifact '{name}'.")

        artifact_id: uuid.UUID = row.id

        if row.category != category:
            raise ArtifactCategoryConflictError(
                f"Artifact '{name}' already exists as a '{row.category}'; "
                f"cannot register as a '{category}'."
            )

        return artifact_id

    async def get_existing_auto_versions(
        self,
        artifact_id: uuid.UUID,
    ) -> Sequence[str]:
        """Return version strings for rows whose version matches ``^v\\d+$``."""
        stmt = select(ArtifactVersion.version).where(
            ArtifactVersion.artifact_id == artifact_id,
            ArtifactVersion.version.regexp_match(r"^v\d+$"),
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def insert_artifact_version(
        self,
        artifact_id: uuid.UUID,
        version: str,
        harbor_ref: str,
        digest: str | None,
        size_bytes: int | None,
        metadata: dict[str, Any],
    ) -> None:
        """Insert a row into ``artifact_versions``.

        Raises :exc:`~app.exceptions.VersionAlreadyExistsError` on a unique
        constraint violation on ``(artifact_id, version)``.
        """
        obj = ArtifactVersion(
            artifact_id=artifact_id,
            version=version,
            harbor_ref=harbor_ref,
            digest=digest,
            size_bytes=size_bytes,
            metadata_=metadata,
        )
        self._session.add(obj)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            raise VersionAlreadyExistsError(
                f"Version '{version}' already exists for artifact id {artifact_id}."
            ) from exc

    async def touch_artifact_updated_at(self, artifact_id: uuid.UUID) -> None:
        """Set ``updated_at = now()`` on the artifact row."""
        stmt = (
            update(Artifact)
            .where(Artifact.id == artifact_id)
            .values(updated_at=func.now())
        )
        await self._session.execute(stmt)

    async def get_model_version(self, name: str, version: str) -> ArtifactVersionRecord:
        """Fetch one model version by exact tag or by the ``latest`` alias.

        Raises
        ------
        ModelNotFoundError
            When a model artifact with *name* does not exist.
        ModelVersionNotFoundError
            When the requested *version* does not exist for the model.
        """
        return await self._get_artifact_version(
            name=name,
            version=version,
            category="model",
            not_found_exc=ModelNotFoundError,
            version_not_found_exc=ModelVersionNotFoundError,
        )

    async def get_dataset_version(
        self, name: str, version: str
    ) -> ArtifactVersionRecord:
        """Fetch one dataset version by exact tag or by the ``latest`` alias.

        Raises
        ------
        DatasetNotFoundError
            When a dataset artifact with *name* does not exist.
        DatasetVersionNotFoundError
            When the requested *version* does not exist for the dataset.
        """
        return await self._get_artifact_version(
            name=name,
            version=version,
            category="dataset",
            not_found_exc=DatasetNotFoundError,
            version_not_found_exc=DatasetVersionNotFoundError,
        )

    async def _get_artifact_version(
        self,
        *,
        name: str,
        version: str,
        category: ArtifactCategory,
        not_found_exc: type[Exception],
        version_not_found_exc: type[Exception],
    ) -> ArtifactVersionRecord:
        """Shared lookup for model and dataset version queries.

        Uses a single JOIN query for the happy path.  A lightweight second
        query runs only on miss to distinguish "artifact not found" from
        "version not found".
        """
        label = category.capitalize()

        base = (
            select(
                ArtifactVersion.version,
                ArtifactVersion.harbor_ref,
                ArtifactVersion.size_bytes,
                ArtifactVersion.digest,
                ArtifactVersion.created_at,
                ArtifactVersion.metadata_,
            )
            .join(Artifact, ArtifactVersion.artifact_id == Artifact.id)
            .where(Artifact.name == name, Artifact.category == category)
        )

        if version == "latest":
            stmt = base.order_by(ArtifactVersion.created_at.desc()).limit(1)
        else:
            stmt = base.where(ArtifactVersion.version == version)

        result = await self._session.execute(stmt)
        row = result.fetchone()

        if row is not None:
            return ArtifactVersionRecord(
                name=name,
                version=row.version,
                category=category,
                harbor_ref=row.harbor_ref,
                size_bytes=row.size_bytes,
                checksum=row.digest,
                created_at=row.created_at,
                metadata=row.metadata_ or {},
            )

        exists_result = await self._session.execute(
            select(Artifact.id, Artifact.category).where(Artifact.name == name)
        )
        exists_row = exists_result.fetchone()

        if exists_row is None or exists_row.category != category:
            raise not_found_exc(f"{label} '{name}' was not found.")

        if version == "latest":
            raise version_not_found_exc(f"{label} '{name}' has no versions.")

        raise version_not_found_exc(
            f"Version '{version}' was not found for {category} '{name}'."
        )
