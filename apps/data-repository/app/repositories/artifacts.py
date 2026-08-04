"""SQL access layer for artifact and artifact_version rows.

The canonical interface is :class:`ArtifactRepository`, constructed with an
:class:`~sqlalchemy.ext.asyncio.AsyncSession` whose transaction the caller
owns.

No Harbor imports, no HTTPException references, no business logic beyond
executing SQL.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import (
    String,
    and_,
    cast,
    delete,
    func,
    lateral,
    or_,
    select,
    true,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import catalog_search
from app.database.models import Artifact, ArtifactVersion
from app.exceptions import (
    ArtifactCategoryConflictError,
    CatalogArtifactNotFoundError,
    CatalogVersionNotFoundError,
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


@dataclass(frozen=True)
class ArtifactListRecord:
    """Record for artifact list endpoints."""

    name: str
    category: ArtifactCategory
    description: str | None
    versions_count: int
    latest_version: str | None
    created_at: datetime


@dataclass(frozen=True)
class ArtifactMetadataRecord:
    name: str
    category: str
    description: str | None
    created_at: datetime
    versions_count: int
    metadata: dict[str, Any] = field(default_factory=dict)


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

    async def list_model_versions(self, name: str) -> list[ArtifactVersionRecord]:
        """List all model versions sorted by ``created_at`` descending.

        Raises
        ------
        ModelNotFoundError
            When a model artifact with *name* does not exist.
        """
        return await self._list_artifact_versions(
            name=name,
            category="model",
            not_found_exc=ModelNotFoundError,
        )

    async def delete_model_version(self, name: str, version: str) -> None:
        """Delete a single model version row.

        Raises
        ------
        ModelNotFoundError
            When a model artifact with *name* does not exist.
        ModelVersionNotFoundError
            When the requested *version* does not exist for the model.
        """
        await self._delete_artifact_version(
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

    async def list_dataset_versions(self, name: str) -> list[ArtifactVersionRecord]:
        """List all dataset versions sorted by ``created_at`` descending.

        Raises
        ------
        DatasetNotFoundError
            When a dataset artifact with *name* does not exist.
        """
        return await self._list_artifact_versions(
            name=name,
            category="dataset",
            not_found_exc=DatasetNotFoundError,
        )

    async def delete_dataset_version(self, name: str, version: str) -> None:
        """Delete a single dataset version row.

        Raises
        ------
        DatasetNotFoundError
            When a dataset artifact with *name* does not exist.
        DatasetVersionNotFoundError
            When the requested *version* does not exist for the dataset.
        """
        await self._delete_artifact_version(
            name=name,
            version=version,
            category="dataset",
            not_found_exc=DatasetNotFoundError,
            version_not_found_exc=DatasetVersionNotFoundError,
        )

    async def resolve_artifact_version(
        self, name: str, version: str
    ) -> ArtifactVersionRecord:
        """Resolve an artifact version without category enforcement.

        Used for repo:// URI resolution.

        Raises
        ------
        CatalogArtifactNotFoundError
            When no artifact with *name* exists.
        CatalogVersionNotFoundError
            When *version* does not exist for the artifact.
        """
        label = "Artifact"
        base = (
            select(
                ArtifactVersion.version,
                ArtifactVersion.harbor_ref,
                ArtifactVersion.size_bytes,
                ArtifactVersion.digest,
                ArtifactVersion.created_at,
                ArtifactVersion.metadata_,
                Artifact.category,
            )
            .join(Artifact, ArtifactVersion.artifact_id == Artifact.id)
            .where(Artifact.name == name)
        )

        if version == "latest":
            stmt = base.order_by(
                ArtifactVersion.created_at.desc(),
                ArtifactVersion.id.desc(),
            ).limit(1)
        else:
            stmt = base.where(ArtifactVersion.version == version)

        result = await self._session.execute(stmt)
        row = result.fetchone()

        if row is not None:
            return ArtifactVersionRecord(
                name=name,
                version=row.version,
                category=row.category,
                harbor_ref=row.harbor_ref,
                size_bytes=row.size_bytes,
                checksum=row.digest,
                created_at=row.created_at,
                metadata=row.metadata_ or {},
            )

        exists_result = await self._session.execute(
            select(Artifact.id).where(Artifact.name == name)
        )
        if exists_result.fetchone() is None:
            raise CatalogArtifactNotFoundError(f"{label} '{name}' was not found.")

        if version == "latest":
            raise CatalogVersionNotFoundError(f"{label} '{name}' has no versions.")

        raise CatalogVersionNotFoundError(
            f"Version '{version}' was not found for artifact '{name}'."
        )

    async def get_artifact_metadata(
        self,
        *,
        category: ArtifactCategory,
        name: str,
    ) -> ArtifactMetadataRecord:
        artifact_row = await self._get_artifact_row(name=name, category=category)

        versions_count_result = await self._session.execute(
            select(func.count())
            .select_from(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == artifact_row.id)
        )
        versions_count = int(versions_count_result.scalar_one())

        latest_result = await self._session.execute(
            select(ArtifactVersion.metadata_)
            .where(ArtifactVersion.artifact_id == artifact_row.id)
            .order_by(
                ArtifactVersion.created_at.desc(),
                ArtifactVersion.id.desc(),
            )
            .limit(1)
        )
        latest_row = latest_result.fetchone()

        return ArtifactMetadataRecord(
            name=artifact_row.name,
            category=artifact_row.category,
            description=artifact_row.description,
            created_at=artifact_row.created_at,
            versions_count=versions_count,
            metadata=(latest_row.metadata_ or {}) if latest_row is not None else {},
        )

    async def update_artifact_metadata(
        self,
        *,
        category: ArtifactCategory,
        name: str,
        description: str | None,
        set_description: bool,
        raw_metadata: dict[str, Any] | None,
        set_metadata: bool,
    ) -> ArtifactMetadataRecord:
        artifact_row = await self._get_artifact_row(name=name, category=category)

        if set_description:
            await self._session.execute(
                update(Artifact)
                .where(Artifact.id == artifact_row.id)
                .values(
                    description=description,
                    updated_at=func.now(),
                )
            )

        if set_metadata:
            latest_version_result = await self._session.execute(
                select(ArtifactVersion.id)
                .where(ArtifactVersion.artifact_id == artifact_row.id)
                .order_by(
                    ArtifactVersion.created_at.desc(),
                    ArtifactVersion.id.desc(),
                )
                .limit(1)
            )
            latest_version_row = latest_version_result.fetchone()
            if latest_version_row is not None:
                await self._session.execute(
                    update(ArtifactVersion)
                    .where(ArtifactVersion.id == latest_version_row.id)
                    .values(metadata_=(raw_metadata or {}))
                )
        return await self.get_artifact_metadata(category=category, name=name)

    async def artifact_version_reference_exists(
        self,
        *,
        name: str,
        version: str,
        category: ArtifactCategory | None = None,
    ) -> bool:
        stmt = (
            select(ArtifactVersion.id)
            .join(Artifact, ArtifactVersion.artifact_id == Artifact.id)
            .where(
                Artifact.name == name,
                ArtifactVersion.version == version,
            )
        )
        if category is not None:
            stmt = stmt.where(Artifact.category == category)

        result = await self._session.execute(stmt.limit(1))
        return result.fetchone() is not None

    async def update_artifact_version_metadata(
        self,
        *,
        category: ArtifactCategory,
        name: str,
        version: str,
        metadata: dict[str, Any],
    ) -> ArtifactVersionRecord:
        current = await self._get_artifact_version(
            name=name,
            version=version,
            category=category,
            not_found_exc=self._artifact_not_found_exception(category),
            version_not_found_exc=self._artifact_version_not_found_exception(category),
        )

        stmt = (
            update(ArtifactVersion)
            .where(
                ArtifactVersion.version == current.version,
                ArtifactVersion.artifact_id.in_(
                    select(Artifact.id).where(
                        Artifact.name == name,
                        Artifact.category == category,
                    )
                ),
            )
            .values(metadata_=metadata)
            .returning(
                ArtifactVersion.version,
                ArtifactVersion.harbor_ref,
                ArtifactVersion.size_bytes,
                ArtifactVersion.digest,
                ArtifactVersion.created_at,
                ArtifactVersion.metadata_,
            )
        )
        result = await self._session.execute(stmt)
        row = result.fetchone()
        if row is None:
            raise self._artifact_version_not_found_exception(category)(
                f"Version '{current.version}' was not found for {category} '{name}'."
            )

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

    async def _get_artifact_row(
        self,
        *,
        name: str,
        category: ArtifactCategory,
    ) -> Row[Any]:
        result = await self._session.execute(
            select(
                Artifact.id,
                Artifact.name,
                Artifact.category,
                Artifact.description,
                Artifact.created_at,
            ).where(
                Artifact.name == name,
                Artifact.category == category,
            )
        )
        row = result.fetchone()
        if row is not None:
            return row

        exists_row = await self._fetch_artifact_identity(name)
        if exists_row is None or exists_row.category != category:
            label = category.capitalize()
            raise self._artifact_not_found_exception(category)(
                f"{label} '{name}' was not found."
            )

        # Defensive fallback for a stale read between checks.
        label = category.capitalize()
        raise self._artifact_not_found_exception(category)(
            f"{label} '{name}' was not found."
        )

    @staticmethod
    def _artifact_not_found_exception(
        category: ArtifactCategory,
    ) -> type[ModelNotFoundError] | type[DatasetNotFoundError]:
        if category == "model":
            return ModelNotFoundError
        return DatasetNotFoundError

    @staticmethod
    def _artifact_version_not_found_exception(
        category: ArtifactCategory,
    ) -> type[ModelVersionNotFoundError] | type[DatasetVersionNotFoundError]:
        if category == "model":
            return ModelVersionNotFoundError
        return DatasetVersionNotFoundError

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
            stmt = base.order_by(
                ArtifactVersion.created_at.desc(),
                ArtifactVersion.id.desc(),
            ).limit(1)
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

        exists_row = await self._fetch_artifact_identity(name)

        if exists_row is None or exists_row.category != category:
            raise not_found_exc(f"{label} '{name}' was not found.")

        if version == "latest":
            raise version_not_found_exc(f"{label} '{name}' has no versions.")

        raise version_not_found_exc(
            f"Version '{version}' was not found for {category} '{name}'."
        )

    async def _list_artifact_versions(
        self,
        *,
        name: str,
        category: ArtifactCategory,
        not_found_exc: type[Exception],
    ) -> list[ArtifactVersionRecord]:
        stmt = (
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
            .order_by(
                ArtifactVersion.created_at.desc(),
                ArtifactVersion.id.desc(),
            )
        )
        result = await self._session.execute(stmt)
        rows = result.fetchall()

        if rows:
            return [
                ArtifactVersionRecord(
                    name=name,
                    version=row.version,
                    category=category,
                    harbor_ref=row.harbor_ref,
                    size_bytes=row.size_bytes,
                    checksum=row.digest,
                    created_at=row.created_at,
                    metadata=row.metadata_ or {},
                )
                for row in rows
            ]

        exists_row = await self._fetch_artifact_identity(name)
        if exists_row is None or exists_row.category != category:
            label = category.capitalize()
            raise not_found_exc(f"{label} '{name}' was not found.")

        return []

    async def _delete_artifact_version(
        self,
        *,
        name: str,
        version: str,
        category: ArtifactCategory,
        not_found_exc: type[Exception],
        version_not_found_exc: type[Exception],
    ) -> None:
        """Shared delete for model and dataset versions.

        Attempts a single DELETE filtered by artifact name and category.  When
        nothing was deleted, a lightweight existence query disambiguates
        "artifact not found" from "version not found".  Harbor-side deletion
        is intentionally not performed here — see N-029 for retention policy.
        """
        stmt = delete(ArtifactVersion).where(
            ArtifactVersion.version == version,
            ArtifactVersion.artifact_id.in_(
                select(Artifact.id).where(
                    Artifact.name == name,
                    Artifact.category == category,
                )
            ),
        )
        result = await self._session.execute(stmt)
        if result.rowcount:
            return

        label = category.capitalize()
        exists_row = await self._fetch_artifact_identity(name)
        if exists_row is None or exists_row.category != category:
            raise not_found_exc(f"{label} '{name}' was not found.")

        raise version_not_found_exc(
            f"Version '{version}' was not found for {category} '{name}'."
        )

    async def _fetch_artifact_identity(self, name: str) -> Row[Any] | None:
        exists_result = await self._session.execute(
            select(Artifact.id, Artifact.category).where(Artifact.name == name)
        )
        return exists_result.fetchone()

    @staticmethod
    def _artifact_list_search_conditions(
        *,
        search: str | None,
        version_stats: Any,
    ) -> list[Any]:
        """Build extra ``WHERE`` fragments for name, description, and metadata."""
        if not search:
            return []
        pattern = catalog_search.ilike_substring_pattern(search)
        text_match = (
            Artifact.name.ilike(pattern)
            | Artifact.description.ilike(pattern)
            | cast(version_stats.c.latest_metadata, String).ilike(pattern)
        )
        try:
            parsed = json.loads(search)
        except json.JSONDecodeError:
            return [text_match]
        if isinstance(parsed, dict) and parsed:
            return [
                or_(
                    text_match,
                    and_(
                        version_stats.c.latest_metadata.isnot(None),
                        version_stats.c.latest_metadata.contains(parsed),
                    ),
                )
            ]
        return [text_match]

    async def list_artifacts_by_category(
        self,
        category: ArtifactCategory,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[int, Sequence[ArtifactListRecord]]:
        """List artifacts by category with optional search and pagination.

        Uses a single database round-trip: a count over the filtered set plus
        a ``LATERAL`` page slice, so ``total`` is correct even when ``offset``
        lies past the last row.

        Search matches (OR): artifact ``name``, ``description`` (``ILIKE``),
        the **latest** version's ``metadata`` as text (``ILIKE``), and when
        *search* parses as a JSON object, ``metadata @>`` that object.

        Parameters
        ----------
        category
            The artifact category to filter by (``model`` or ``dataset``).
        search
            Optional search term (see module :mod:`app.catalog_search`).
        limit
            Maximum number of results to return (default: 50).
        offset
            Number of results to skip for pagination (default: 0).

        Returns
        -------
        tuple[int, Sequence[ArtifactListRecord]]
            ``(total, records)`` where *total* counts matching artifacts.
        """
        version_counts = (
            select(
                ArtifactVersion.artifact_id.label("artifact_id"),
                func.count(ArtifactVersion.id).label("versions_count"),
            )
            .group_by(ArtifactVersion.artifact_id)
            .subquery()
        )

        ranked_versions = (
            select(
                ArtifactVersion.artifact_id.label("artifact_id"),
                ArtifactVersion.version.label("latest_version"),
                ArtifactVersion.metadata_.label("latest_metadata"),
                func.row_number()
                .over(
                    partition_by=ArtifactVersion.artifact_id,
                    order_by=(
                        ArtifactVersion.created_at.desc(),
                        ArtifactVersion.id.desc(),
                    ),
                )
                .label("version_rank"),
            )
        ).subquery()

        version_stats = (
            select(
                version_counts.c.artifact_id,
                version_counts.c.versions_count,
                ranked_versions.c.latest_version,
                ranked_versions.c.latest_metadata,
            )
            .join(
                ranked_versions,
                and_(
                    version_counts.c.artifact_id == ranked_versions.c.artifact_id,
                    ranked_versions.c.version_rank == 1,
                ),
            )
            .subquery()
        )

        base_conditions = [Artifact.category == category]
        base_conditions.extend(
            self._artifact_list_search_conditions(
                search=search, version_stats=version_stats
            )
        )

        count_from = (
            select(func.count(Artifact.id).label("match_total"))
            .select_from(Artifact)
            .outerjoin(version_stats, Artifact.id == version_stats.c.artifact_id)
            .where(and_(*base_conditions))
        ).subquery()

        page_inner = (
            select(
                Artifact.name,
                Artifact.category,
                Artifact.description,
                version_stats.c.versions_count,
                version_stats.c.latest_version,
                Artifact.created_at,
            )
            .select_from(Artifact)
            .outerjoin(version_stats, Artifact.id == version_stats.c.artifact_id)
            .where(and_(*base_conditions))
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            .limit(limit)
            .offset(offset)
        )

        page_lat = lateral(page_inner).alias("p")

        stmt = select(
            count_from.c.match_total,
            page_lat.c.name,
            page_lat.c.category,
            page_lat.c.description,
            page_lat.c.versions_count,
            page_lat.c.latest_version,
            page_lat.c.created_at,
        ).select_from(count_from.outerjoin(page_lat, true()))

        result = await self._session.execute(stmt)
        rows = result.fetchall()
        if not rows:
            return 0, []

        total = int(rows[0].match_total)
        records: list[ArtifactListRecord] = []
        for row in rows:
            if row.name is None:
                continue
            records.append(
                ArtifactListRecord(
                    name=row.name,
                    category=row.category,
                    description=row.description,
                    versions_count=row.versions_count or 0,
                    latest_version=row.latest_version,
                    created_at=row.created_at,
                )
            )
        return total, records
