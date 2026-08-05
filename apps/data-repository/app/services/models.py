"""Service layer for artifact version registration, querying, and deletion.

Orchestrates input validation, Harbor verification, and the DB transaction.
Raises only domain exceptions from :mod:`app.exceptions`; callers map those to
HTTP status codes.

This module exposes six services:
- :class:`ModelRegistrationService` for model write operations (registration)
- :class:`DatasetRegistrationService` for dataset write operations (registration)
- :class:`ModelQueryService` for model read operations (version lookup/listing)
- :class:`DatasetQueryService` for dataset read operations (version lookup/listing)
- :class:`ModelDeletionService` for model write operations (version removal)
- :class:`DatasetDeletionService` for dataset write operations (version removal)

Registration services take a :class:`~harbor_oci_client.HarborClient` (for
pre-flight verification) and an :class:`~sqlalchemy.ext.asyncio.AsyncSession`.
Query and deletion services take only an
:class:`~sqlalchemy.ext.asyncio.AsyncSession`. Shared registration logic is
kept in :class:`BaseArtifactRegistrationService`; shared deletion logic is
kept in :class:`BaseArtifactDeletionService`.

Routes use dependency providers in :mod:`app.dependencies`; the services never
call global singleton accessors.
"""

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog_search import normalize_artifact_list_search
from app.exceptions import (
    ArtifactNotFoundInHarborError,
    HarborVerificationError,
    InvalidArtifactNameError,
    InvalidLineageReferenceError,
    LineageReferenceNotFoundError,
)
from app.harbor import (
    ArtifactNotFoundError,
    HarborAPIError,
    HarborAuthError,
    HarborClient,
    HarborConnectionError,
)
from app.repositories.artifacts import ArtifactMetadataRecord, ArtifactRepository
from app.schemas.artifacts import ArtifactListResponse, ArtifactSummary
from app.schemas.datasets import (
    DatasetVersionListItem,
    GetDatasetMetadataResponse,
    GetDatasetVersionResponse,
    ListDatasetVersionsResponse,
    RegisterDatasetVersionRequest,
    RegisterDatasetVersionResponse,
    UpdateDatasetMetadataRequest,
    UpdateDatasetVersionRequest,
    UpdateDatasetVersionResponse,
)
from app.schemas.models import (
    GetModelMetadataResponse,
    GetModelVersionResponse,
    LineageMetadata,
    ListModelVersionsResponse,
    ModelVersionListItem,
    RegisterModelVersionRequest,
    RegisterModelVersionResponse,
    UpdateModelMetadataRequest,
    UpdateModelVersionRequest,
    UpdateModelVersionResponse,
)
from app.types import ArtifactCategory

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,254}$")
_ARTIFACT_REF_RE = re.compile(
    r"^(?P<name>[a-z0-9][a-z0-9._-]{0,254}):"
    r"(?P<version>[A-Za-z0-9][A-Za-z0-9._-]{0,127})$"
)


def _extract_metadata_sections(
    metadata: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
    training_config = _extract_object_metadata(metadata, "training_config")
    eval_metrics = _extract_object_metadata(metadata, "eval_metrics")
    lineage = _extract_object_metadata(metadata, "lineage")
    return training_config, eval_metrics, lineage


def _validate_artifact_name(name: str) -> None:
    """Validate artifact name format and length.

    Raises
    ------
    InvalidArtifactNameError
        When *name* fails the length or character-set rules.
    """
    if len(name) > 255 or not _NAME_RE.match(name):
        raise InvalidArtifactNameError(
            "Artifact name must be 1\u2013255 characters and contain only "
            "lowercase alphanumeric characters, hyphens, underscores, or dots."
        )


def _parse_artifact_reference(ref: str) -> tuple[str, str]:
    match = _ARTIFACT_REF_RE.fullmatch(ref)
    if match is None:
        raise InvalidLineageReferenceError(
            f"Lineage reference '{ref}' must use 'name:version' format."
        )
    name = match.group("name")
    version = match.group("version")
    if version.lower() == "latest":
        raise InvalidLineageReferenceError(
            "Lineage references must use an exact version, not 'latest'."
        )
    return name, version


async def _validate_lineage_references(
    repo: ArtifactRepository,
    lineage: dict[str, str],
) -> None:
    fields_to_validate: tuple[tuple[str, ArtifactCategory], ...] = (
        ("parent_model", "model"),
        ("source_dataset", "dataset"),
    )

    for field_name, category in fields_to_validate:
        ref = lineage.get(field_name)
        if ref is None:
            continue

        name, version = _parse_artifact_reference(ref)
        exists = await repo.artifact_version_reference_exists(
            name=name,
            version=version,
            category=category,
        )
        if not exists:
            raise LineageReferenceNotFoundError(
                f"Lineage reference '{ref}' for '{field_name}' was not found."
            )


def _extract_object_metadata(
    metadata: dict[str, Any],
    key: str,
) -> dict[str, Any] | None:
    """Project a top-level JSONB field as a dict, or ``None`` if absent/non-object.

    Scalars and arrays at *key* are intentionally dropped so the response stays
    strictly typed. The metadata conventions in :doc:`/docs/schema.md` define
    these top-level fields as objects.
    """
    value = metadata.get(key)
    return value if isinstance(value, dict) else None


def _to_model_metadata_response(
    record: ArtifactMetadataRecord,
) -> GetModelMetadataResponse:
    training_config, eval_metrics, lineage = _extract_metadata_sections(record.metadata)

    return GetModelMetadataResponse(
        name=record.name,
        category=record.category,
        description=record.description,
        training_config=training_config,
        eval_metrics=eval_metrics,
        lineage=(
            LineageMetadata.model_validate(lineage) if lineage is not None else None
        ),
        created_at=record.created_at,
        versions_count=record.versions_count,
    )


def _to_dataset_metadata_response(
    record: ArtifactMetadataRecord,
) -> GetDatasetMetadataResponse:
    training_config, eval_metrics, lineage = _extract_metadata_sections(record.metadata)

    return GetDatasetMetadataResponse(
        name=record.name,
        category=record.category,
        description=record.description,
        training_config=training_config,
        eval_metrics=eval_metrics,
        lineage=(
            LineageMetadata.model_validate(lineage) if lineage is not None else None
        ),
        created_at=record.created_at,
        versions_count=record.versions_count,
    )


class MetadataUpdateRequest(Protocol):
    description: str | None
    training_config: dict[str, Any] | None
    eval_metrics: dict[str, Any] | None
    lineage: LineageMetadata | None
    model_fields_set: set[str]


async def _update_artifact_metadata(
    repo: ArtifactRepository,
    *,
    category: ArtifactCategory,
    name: str,
    request: MetadataUpdateRequest,
) -> ArtifactMetadataRecord:
    _validate_artifact_name(name)

    current = await repo.get_artifact_metadata(category=category, name=name)
    merged_metadata = dict(current.metadata)

    lineage_dict: dict[str, str] | None = None
    should_set_lineage = "lineage" in request.model_fields_set
    if should_set_lineage and request.lineage is not None:
        lineage_dict = cast(
            dict[str, str],
            request.lineage.model_dump(exclude_none=True),
        )
        await _validate_lineage_references(repo, lineage_dict)

    if "training_config" in request.model_fields_set:
        if request.training_config is None:
            merged_metadata.pop("training_config", None)
        else:
            merged_metadata["training_config"] = request.training_config

    if "eval_metrics" in request.model_fields_set:
        if request.eval_metrics is None:
            merged_metadata.pop("eval_metrics", None)
        else:
            merged_metadata["eval_metrics"] = request.eval_metrics

    if should_set_lineage:
        if lineage_dict is None:
            merged_metadata.pop("lineage", None)
        else:
            merged_metadata["lineage"] = lineage_dict

    set_metadata = any(
        key in request.model_fields_set
        for key in ("training_config", "eval_metrics", "lineage")
    )

    return await repo.update_artifact_metadata(
        category=category,
        name=name,
        description=request.description,
        set_description="description" in request.model_fields_set,
        raw_metadata=merged_metadata if set_metadata else None,
        set_metadata=set_metadata,
    )


class ArtifactRegistrationRequest(Protocol):
    harbor_ref: str
    version: str | None
    checksum: str | None
    size_bytes: int | None
    metadata: dict[str, Any] | BaseModel | None


@dataclass(frozen=True)
class RegistrationResult:
    name: str
    version: str
    harbor_ref: str
    category: ArtifactCategory


class BaseArtifactRegistrationService:
    """Shared Harbor verify + persistence flow for artifact registrations."""

    def __init__(self, harbor: HarborClient, session: AsyncSession) -> None:
        self._harbor = harbor
        self._session = session
        self._repo = ArtifactRepository(session)

    async def register_artifact_version(
        self,
        name: str,
        request: ArtifactRegistrationRequest,
        *,
        category: ArtifactCategory,
    ) -> RegistrationResult:
        _validate_artifact_name(name)

        if request.version is not None and request.version.lower() == "latest":
            raise InvalidArtifactNameError(
                "'latest' is a reserved alias and cannot be used as a version tag."
            )

        try:
            info = await self._harbor.verify_artifact(request.harbor_ref)
        except ArtifactNotFoundError:
            raise ArtifactNotFoundInHarborError(
                f"Artifact '{request.harbor_ref}' was not found in Harbor."
            )
        except (HarborAuthError, HarborConnectionError, HarborAPIError) as exc:
            logger.error(
                "Harbor error during verify_artifact for '%s': %s",
                request.harbor_ref,
                exc,
            )
            raise HarborVerificationError(f"Harbor error: {exc}")

        digest = request.checksum or info.digest
        size_bytes = request.size_bytes or info.content_length
        metadata = self._to_metadata_dict(request.metadata)

        artifact_id = await self._repo.upsert_or_fetch_artifact(name, category=category)

        if request.version is not None:
            version = request.version
        else:
            existing = await self._repo.get_existing_auto_versions(artifact_id)
            if not existing:
                version = "v1"
            else:
                nums = [int(v[1:]) for v in existing]
                version = f"v{max(nums) + 1}"

        await self._repo.insert_artifact_version(
            artifact_id,
            version,
            request.harbor_ref,
            digest,
            size_bytes,
            metadata,
        )
        await self._repo.touch_artifact_updated_at(artifact_id)

        # Commit INSIDE the service, before the response is returned. The
        # ``get_db_session`` dependency commits in its post-response
        # teardown, which races the next request: a client that immediately
        # reads its own write (e.g. the upload pre-flight conflict check)
        # can observe a 404 for a version the API just reported as created.
        await self._session.commit()

        return RegistrationResult(
            name=name,
            version=version,
            harbor_ref=request.harbor_ref,
            category=category,
        )

    @staticmethod
    def _to_metadata_dict(
        metadata: dict[str, Any] | BaseModel | None,
    ) -> dict[str, Any]:
        if metadata is None:
            return {}
        if isinstance(metadata, dict):
            return metadata
        return metadata.model_dump(exclude_none=True)


class ModelRegistrationService(BaseArtifactRegistrationService):
    """Orchestrates model-version registration using injected dependencies.

    Receives a :class:`~harbor_oci_client.HarborClient` and an
    :class:`~sqlalchemy.ext.asyncio.AsyncSession` via the constructor.  An
    :class:`~app.repositories.artifacts.ArtifactRepository` is built
    internally from the session so callers never construct it themselves.

    The transaction boundary is owned by the caller (e.g. the
    ``get_db_session`` FastAPI dependency).
    """

    async def register_model_version(
        self,
        name: str,
        request: RegisterModelVersionRequest,
    ) -> RegisterModelVersionResponse:
        """Validate, verify in Harbor, and persist a new model artifact version.

        Raises
        ------
        InvalidArtifactNameError
            When *name* fails the length or character-set rules.
        ArtifactNotFoundInHarborError
            When the Harbor reference in *request.harbor_ref* cannot be resolved.
        HarborVerificationError
            When Harbor returns an auth, connection, or API-level error.
        ArtifactCategoryConflictError
            When an artifact with *name* already exists with a different category.
        VersionAlreadyExistsError
            When the resolved version already exists for this artifact.
        """
        result = await self.register_artifact_version(
            name,
            request,
            category="model",
        )

        return RegisterModelVersionResponse(
            name=result.name,
            version=result.version,
            harbor_ref=result.harbor_ref,
            category=result.category,
        )


class DatasetRegistrationService(BaseArtifactRegistrationService):
    """Orchestrates dataset-version registration using injected dependencies."""

    async def register_dataset_version(
        self,
        name: str,
        request: RegisterDatasetVersionRequest,
    ) -> RegisterDatasetVersionResponse:
        result = await self.register_artifact_version(
            name,
            request,
            category="dataset",
        )

        return RegisterDatasetVersionResponse(
            name=result.name,
            version=result.version,
            harbor_ref=result.harbor_ref,
            category=result.category,
        )


class ModelQueryService:
    """Read-only model query operations using injected dependencies."""

    def __init__(self, session: AsyncSession) -> None:
        self._repo = ArtifactRepository(session)

    async def get_model_version(
        self, name: str, version: str
    ) -> GetModelVersionResponse:
        """Resolve and return one model version by tag or ``latest`` alias.

        Raises
        ------
        InvalidArtifactNameError
            When *name* fails the length or character-set rules.
        ModelNotFoundError
            When model artifact *name* does not exist.
        ModelVersionNotFoundError
            When *version* does not exist for model *name*.
        """
        _validate_artifact_name(name)

        record = await self._repo.get_model_version(name=name, version=version)

        return GetModelVersionResponse(
            name=record.name,
            version=record.version,
            category=record.category,
            harbor_ref=record.harbor_ref,
            size_bytes=record.size_bytes,
            checksum=record.checksum,
            created_at=record.created_at,
            metadata=record.metadata,
        )

    async def list_model_versions(self, name: str) -> ListModelVersionsResponse:
        """List all model versions ordered from newest to oldest."""
        _validate_artifact_name(name)

        records = await self._repo.list_model_versions(name=name)
        items = [
            ModelVersionListItem(
                version=record.version,
                harbor_ref=record.harbor_ref,
                created_at=record.created_at,
                size_bytes=record.size_bytes,
                checksum=record.checksum,
                training_config=_extract_object_metadata(
                    record.metadata,
                    "training_config",
                ),
                eval_metrics=_extract_object_metadata(
                    record.metadata,
                    "eval_metrics",
                ),
            )
            for record in records
        ]
        return ListModelVersionsResponse(versions=items)

    async def list_models(
        self,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ArtifactListResponse[ArtifactSummary]:
        """List models with optional search and pagination.

        Parameters
        ----------
        search
            Optional search term to filter by name or description (case-insensitive).
        limit
            Maximum number of results to return (default: 50).
        offset
            Number of results to skip for pagination (default: 0).

        Returns
        -------
        ArtifactListResponse[ArtifactSummary]
            Paginated list of models with version counts and latest version.
        """
        normalized = normalize_artifact_list_search(search)
        total, records = await self._repo.list_artifacts_by_category(
            "model", search=normalized, limit=limit, offset=offset
        )

        items = [
            ArtifactSummary(
                name=record.name,
                category=record.category,
                description=record.description,
                versions_count=record.versions_count,
                latest_version=record.latest_version,
                created_at=record.created_at,
            )
            for record in records
        ]

        return ArtifactListResponse(total=total, items=items)

    async def get_model_metadata(self, name: str) -> GetModelMetadataResponse:
        _validate_artifact_name(name)
        record = await self._repo.get_artifact_metadata(category="model", name=name)
        return _to_model_metadata_response(record)


class DatasetQueryService:
    """Read-only dataset query operations using injected dependencies."""

    def __init__(self, session: AsyncSession) -> None:
        self._repo = ArtifactRepository(session)

    async def get_dataset_version(
        self, name: str, version: str
    ) -> GetDatasetVersionResponse:
        """Resolve and return one dataset version by tag or ``latest`` alias.

        Raises
        ------
        InvalidArtifactNameError
            When *name* fails the length or character-set rules.
        DatasetNotFoundError
            When dataset artifact *name* does not exist.
        DatasetVersionNotFoundError
            When *version* does not exist for dataset *name*.
        """
        _validate_artifact_name(name)

        record = await self._repo.get_dataset_version(name=name, version=version)

        return GetDatasetVersionResponse(
            name=record.name,
            version=record.version,
            category=record.category,
            harbor_ref=record.harbor_ref,
            size_bytes=record.size_bytes,
            checksum=record.checksum,
            created_at=record.created_at,
            metadata=record.metadata,
        )

    async def list_dataset_versions(self, name: str) -> ListDatasetVersionsResponse:
        """List all dataset versions ordered from newest to oldest."""
        _validate_artifact_name(name)

        records = await self._repo.list_dataset_versions(name=name)
        items = [
            DatasetVersionListItem(
                version=record.version,
                harbor_ref=record.harbor_ref,
                created_at=record.created_at,
                size_bytes=record.size_bytes,
                checksum=record.checksum,
            )
            for record in records
        ]
        return ListDatasetVersionsResponse(versions=items)

    async def list_datasets(
        self,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ArtifactListResponse[ArtifactSummary]:
        """List datasets with optional search and pagination.

        Parameters
        ----------
        search
            Optional search term to filter by name or description (case-insensitive).
        limit
            Maximum number of results to return (default: 50).
        offset
            Number of results to skip for pagination (default: 0).

        Returns
        -------
        ArtifactListResponse[ArtifactSummary]
            Paginated list of datasets with version counts and latest version.
        """
        normalized = normalize_artifact_list_search(search)
        total, records = await self._repo.list_artifacts_by_category(
            "dataset", search=normalized, limit=limit, offset=offset
        )

        items = [
            ArtifactSummary(
                name=record.name,
                category=record.category,
                description=record.description,
                versions_count=record.versions_count,
                latest_version=record.latest_version,
                created_at=record.created_at,
            )
            for record in records
        ]

        return ArtifactListResponse(total=total, items=items)

    async def get_dataset_metadata(self, name: str) -> GetDatasetMetadataResponse:
        _validate_artifact_name(name)
        record = await self._repo.get_artifact_metadata(category="dataset", name=name)
        return _to_dataset_metadata_response(record)


class BaseArtifactUpdateService:
    """Shared metadata-update wiring for model and dataset services."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = ArtifactRepository(session)

    async def _commit(self) -> None:
        # The ``get_db_session`` dependency commits in its post-response
        # teardown, which races the next request — mutations must be durable
        # before the response is returned (see register_artifact_version).
        await self._session.commit()


class ModelUpdateService(BaseArtifactUpdateService):
    async def update_model_metadata(
        self,
        name: str,
        request: UpdateModelMetadataRequest,
    ) -> GetModelMetadataResponse:
        record = await _update_artifact_metadata(
            self._repo,
            category="model",
            name=name,
            request=request,
        )
        await self._commit()
        return _to_model_metadata_response(record)

    async def update_model_version(
        self,
        name: str,
        version: str,
        request: UpdateModelVersionRequest,
    ) -> UpdateModelVersionResponse:
        _validate_artifact_name(name)

        current = await self._repo.get_model_version(name=name, version=version)
        merged_metadata = dict(current.metadata)
        merged_metadata.update(request.metadata)

        updated = await self._repo.update_artifact_version_metadata(
            category="model",
            name=name,
            version=current.version,
            metadata=merged_metadata,
        )
        await self._commit()
        return UpdateModelVersionResponse(
            name=updated.name,
            version=updated.version,
            updated_at=datetime.now(UTC),
            metadata=updated.metadata,
        )


class DatasetUpdateService(BaseArtifactUpdateService):
    async def update_dataset_metadata(
        self,
        name: str,
        request: UpdateDatasetMetadataRequest,
    ) -> GetDatasetMetadataResponse:
        record = await _update_artifact_metadata(
            self._repo,
            category="dataset",
            name=name,
            request=request,
        )
        await self._commit()
        return _to_dataset_metadata_response(record)

    async def update_dataset_version(
        self,
        name: str,
        version: str,
        request: UpdateDatasetVersionRequest,
    ) -> UpdateDatasetVersionResponse:
        _validate_artifact_name(name)

        current = await self._repo.get_dataset_version(name=name, version=version)
        merged_metadata = dict(current.metadata)
        merged_metadata.update(request.metadata)

        updated = await self._repo.update_artifact_version_metadata(
            category="dataset",
            name=name,
            version=current.version,
            metadata=merged_metadata,
        )
        await self._commit()
        return UpdateDatasetVersionResponse(
            name=updated.name,
            version=updated.version,
            updated_at=datetime.now(UTC),
            metadata=updated.metadata,
        )


class BaseArtifactDeletionService:
    """Shared version-deletion flow for model and dataset services.

    The session's transaction boundary is owned by the caller (the
    ``get_db_session`` FastAPI dependency), so a failed delete leaves the
    database untouched.  Harbor blob retention is handled separately — see
    N-029 and :doc:`/docs/schema.md`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = ArtifactRepository(session)

    async def _commit(self) -> None:
        # The ``get_db_session`` dependency commits in its post-response
        # teardown, which races the next request — mutations must be durable
        # before the response is returned (see register_artifact_version).
        await self._session.commit()

    @staticmethod
    def _reject_latest_alias(version: str) -> None:
        if version.lower() == "latest":
            raise InvalidArtifactNameError(
                "'latest' is a reserved alias and cannot be deleted; "
                "delete a concrete version tag instead."
            )


class ModelDeletionService(BaseArtifactDeletionService):
    """Remove a single model version row or the whole model artifact."""

    async def delete_model_version(self, name: str, version: str) -> None:
        """Validate *name* / *version* and delete the matching model version.

        Raises
        ------
        InvalidArtifactNameError
            When *name* fails the length or character-set rules, or *version*
            is the reserved ``latest`` alias.
        ModelNotFoundError
            When the model artifact *name* does not exist.
        ModelVersionNotFoundError
            When *version* does not exist for model *name*.
        """
        _validate_artifact_name(name)
        self._reject_latest_alias(version)

        await self._repo.delete_model_version(name=name, version=version)
        await self._commit()

    async def delete_model(self, name: str) -> None:
        """Validate *name* and delete the whole model artifact (versions cascade).

        Raises
        ------
        InvalidArtifactNameError
            When *name* fails the length or character-set rules.
        ModelNotFoundError
            When the model artifact *name* does not exist.
        """
        _validate_artifact_name(name)

        await self._repo.delete_model(name=name)
        await self._commit()


class DatasetDeletionService(BaseArtifactDeletionService):
    """Remove a single dataset version row or the whole dataset artifact."""

    async def delete_dataset_version(self, name: str, version: str) -> None:
        """Validate *name* / *version* and delete the matching dataset version.

        Raises
        ------
        InvalidArtifactNameError
            When *name* fails the length or character-set rules, or *version*
            is the reserved ``latest`` alias.
        DatasetNotFoundError
            When the dataset artifact *name* does not exist.
        DatasetVersionNotFoundError
            When *version* does not exist for dataset *name*.
        """
        _validate_artifact_name(name)
        self._reject_latest_alias(version)

        await self._repo.delete_dataset_version(name=name, version=version)
        await self._commit()

    async def delete_dataset(self, name: str) -> None:
        """Validate *name* and delete the whole dataset artifact (versions cascade).

        Raises
        ------
        InvalidArtifactNameError
            When *name* fails the length or character-set rules.
        DatasetNotFoundError
            When the dataset artifact *name* does not exist.
        """
        _validate_artifact_name(name)

        await self._repo.delete_dataset(name=name)
        await self._commit()
