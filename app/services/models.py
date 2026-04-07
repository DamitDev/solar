"""Service layer for artifact version registration and querying.

Orchestrates input validation, Harbor verification, and the DB transaction.
Raises only domain exceptions from :mod:`app.exceptions`; callers map those to
HTTP status codes.

This module exposes three services:
- :class:`ModelRegistrationService` for model write operations (registration)
- :class:`DatasetRegistrationService` for dataset write operations (registration)
- :class:`ModelQueryService` for read operations (version lookup)

The canonical interfaces are :class:`ModelRegistrationService` and
:class:`DatasetRegistrationService`, both constructed with a
:class:`~harbor_oci_client.HarborClient` and an
:class:`~sqlalchemy.ext.asyncio.AsyncSession`. Shared registration logic is
kept in :class:`BaseArtifactRegistrationService`.

Routes use dependency providers in :mod:`app.dependencies`; the services never
call global singleton accessors.
"""

from dataclasses import dataclass
import logging
import re
from typing import Any, Protocol

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import (
    ArtifactNotFoundInHarborError,
    HarborVerificationError,
    InvalidArtifactNameError,
)
from app.harbor import (
    ArtifactNotFoundError,
    HarborAPIError,
    HarborAuthError,
    HarborClient,
    HarborConnectionError,
)
from app.repositories.artifacts import ArtifactRepository
from app.schemas.datasets import (
    RegisterDatasetVersionRequest,
    RegisterDatasetVersionResponse,
)
from app.schemas.models import (
    GetModelVersionResponse,
    RegisterModelVersionRequest,
    RegisterModelVersionResponse,
)
from app.types import ArtifactCategory

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,254}$")


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
