"""URI resolution routes under /api/resolve."""

from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query

from app.dependencies import get_resolve_service
from app.exceptions import (
    CatalogArtifactNotFoundError,
    CatalogVersionNotFoundError,
    InvalidArtifactNameError,
)
from app.schemas.artifacts import ResolveUriResponse
from app.services.resolve import ResolveService

router = APIRouter(prefix="/api/resolve")


@router.get("", status_code=200)
async def resolve_uri(
    uri: Annotated[
        str,
        Query(..., description="The repo:// URI to resolve (e.g. repo://iris-osl:v3)"),
    ],
    service: Annotated[ResolveService, Depends(get_resolve_service)],
) -> ResolveUriResponse:
    """Resolve a repo:// URI to artifact metadata and a Harbor reference.

    The resolver parses the URI to extract the artifact name and version,
    then looks up the authority (Data Repository) to find the OCI reference
    needed for an ORAS pull.

    Returns 404 if the artifact or version is not found, or 422 if the URI
    format is invalid.
    """
    try:
        return await service.resolve_uri(uri)
    except InvalidArtifactNameError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    except (CatalogArtifactNotFoundError, CatalogVersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=exc.detail)
