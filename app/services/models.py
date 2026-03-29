"""Service layer for model version registration.

Orchestrates input validation, Harbor verification, and the DB transaction.
Raises only domain exceptions from :mod:`app.exceptions`; callers map those to
HTTP status codes.

The canonical interface is :class:`ModelRegistrationService`, constructed with
a :class:`~harbor_oci_client.HarborClient` and an
:class:`~sqlalchemy.ext.asyncio.AsyncSession`.  The service builds a
:class:`~app.repositories.models.ModelArtifactRepository` from the session
(one clear rule: session in → repository constructed internally).

Routes use :func:`app.dependencies.get_model_registration_service` to obtain an
instance via FastAPI ``Depends``; the service itself never calls global singleton
accessors.
"""

import logging
import re
from typing import Any

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
from app.repositories.models import ModelArtifactRepository
from app.schemas.models import RegisterModelVersionRequest, RegisterModelVersionResponse

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,254}$")


class ModelRegistrationService:
    """Orchestrates model-version registration using injected dependencies.

    Receives a :class:`~harbor_oci_client.HarborClient` and an
    :class:`~sqlalchemy.ext.asyncio.AsyncSession` via the constructor.  A
    :class:`~app.repositories.models.ModelArtifactRepository` is built
    internally from the session so callers never construct it themselves.

    The transaction boundary is owned by the caller (e.g. the
    ``get_db_session`` FastAPI dependency).
    """

    def __init__(self, harbor: HarborClient, session: AsyncSession) -> None:
        self._harbor = harbor
        self._repo = ModelArtifactRepository(session)

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
        if len(name) > 255 or not _NAME_RE.match(name):
            raise InvalidArtifactNameError(
                "Artifact name must be 1–255 characters and contain only "
                "lowercase alphanumeric characters, hyphens, underscores, or dots."
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

        metadata: dict[str, Any] = request.metadata or {}

        artifact_id = await self._repo.upsert_or_fetch_artifact(name)

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

        return RegisterModelVersionResponse(
            name=name,
            version=version,
            harbor_ref=request.harbor_ref,
            category="model",
        )
