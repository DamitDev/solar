"""SQL access layer for artifact and artifact_version rows.

The canonical interface is :class:`ModelArtifactRepository`, which is
constructed with an :class:`~sqlalchemy.ext.asyncio.AsyncSession` whose
transaction the caller owns.  Module-level helper functions are thin wrappers
that delegate to the class; they exist only to ease the US-012 migration and
will be removed once the service layer switches to the class directly.

No Harbor imports, no HTTPException references, no business logic beyond
executing SQL.
"""

import uuid
from typing import Any, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Artifact, ArtifactVersion
from app.exceptions import ArtifactCategoryConflictError, VersionAlreadyExistsError


class ModelArtifactRepository:
    """Persistence operations for model artifacts and their versions.

    Construct with an :class:`~sqlalchemy.ext.asyncio.AsyncSession` whose
    transaction boundary is owned by the caller (e.g. the
    ``get_db_session`` FastAPI dependency).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_or_fetch_artifact(self, name: str) -> uuid.UUID:
        """Insert a model artifact if it does not exist, or fetch the existing row.

        Returns the artifact ``id`` when the row has ``category = 'model'``.
        Raises :exc:`~app.exceptions.ArtifactCategoryConflictError` when the
        row already exists with a different category.
        """
        insert_stmt = (
            pg_insert(Artifact)
            .values(name=name, category="model")
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
        category: str = row.category

        if category != "model":
            raise ArtifactCategoryConflictError(
                f"Artifact '{name}' already exists as a '{category}'; "
                "cannot register as a model."
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
