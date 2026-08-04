"""Unified catalog list under ``/api/artifacts``.

Delegates to :class:`~app.services.models.ModelQueryService` or
:class:`~app.services.models.DatasetQueryService` so behaviour matches
``GET /api/models`` and ``GET /api/datasets`` for the same query parameters.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.dependencies import get_dataset_query_service, get_model_query_service
from app.pagination import resolve_list_pagination
from app.routes._artifact_list_params import (
    ARTIFACT_LIST_PAGE_DESCRIPTION,
    ARTIFACT_LIST_PAGE_SIZE_DESCRIPTION,
    ARTIFACT_LIST_SEARCH_DESCRIPTION,
)
from app.schemas.artifacts import ArtifactListResponse, ArtifactSummary
from app.services.models import DatasetQueryService, ModelQueryService

router = APIRouter(prefix="/api/artifacts")


@router.get("", status_code=200)
async def list_artifacts(
    *,
    category: Literal["model", "dataset"] = Query(
        ...,
        description="Filter to ``model`` or ``dataset`` rows (``artifacts.category``).",
    ),
    search: str | None = Query(None, description=ARTIFACT_LIST_SEARCH_DESCRIPTION),
    limit: int = Query(50, ge=1, le=1000, description="Results per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    page: int | None = Query(None, ge=1, description=ARTIFACT_LIST_PAGE_DESCRIPTION),
    page_size: int | None = Query(
        None, ge=1, le=1000, description=ARTIFACT_LIST_PAGE_SIZE_DESCRIPTION
    ),
    model_service: Annotated[ModelQueryService, Depends(get_model_query_service)],
    dataset_service: Annotated[DatasetQueryService, Depends(get_dataset_query_service)],
) -> ArtifactListResponse[ArtifactSummary]:
    """List artifacts with required ``category`` (same semantics as typed routes)."""
    lim, off = resolve_list_pagination(
        limit=limit, offset=offset, page=page, page_size=page_size
    )
    if category == "model":
        return await model_service.list_models(search=search, limit=lim, offset=off)
    return await dataset_service.list_datasets(search=search, limit=lim, offset=off)
