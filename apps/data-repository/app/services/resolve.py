"""Service for repo:// URI resolution."""

import logging
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import InvalidArtifactNameError
from app.repositories.artifacts import ArtifactRepository
from app.schemas.artifacts import ResolveUriResponse
from app.services.models import _validate_artifact_name

logger = logging.getLogger(__name__)

# Matches repo://name:version
_REPO_URI_RE = re.compile(
    r"^repo://(?P<name>[a-z0-9][a-z0-9._-]{0,254}):"
    r"(?P<version>[A-Za-z0-9][A-Za-z0-9._-]{0,127})$"
)


class ResolveService:
    """Authority for repo:// URI resolution."""

    def __init__(self, session: AsyncSession) -> None:
        self._repo = ArtifactRepository(session)

    async def resolve_uri(self, uri: str) -> ResolveUriResponse:
        """Resolve a repo:// URI to artifact metadata and harbor_ref.

        Example: repo://iris-osl:v3 -> ResolveUriResponse(...)

        Raises
        ------
        InvalidArtifactNameError
            When URI format is invalid or name/version fail validation.
        CatalogArtifactNotFoundError
            When artifact name does not exist.
        CatalogVersionNotFoundError
            When version does not exist.
        """
        match = _REPO_URI_RE.match(uri)
        if not match:
            raise InvalidArtifactNameError(
                f"Invalid URI format '{uri}'. Expected 'repo://{{name}}:{{version}}'."
            )

        name = match.group("name")
        version = match.group("version")

        # Reuse existing name validation
        _validate_artifact_name(name)

        record = await self._repo.resolve_artifact_version(name=name, version=version)

        return ResolveUriResponse(
            category=record.category,
            name=record.name,
            version=record.version,
            harbor_ref=record.harbor_ref,
            size_bytes=record.size_bytes,
            checksum=record.checksum,
            metadata=record.metadata,
            created_at=record.created_at,
        )
