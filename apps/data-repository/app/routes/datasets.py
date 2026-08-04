"""Dataset routes under /api/datasets."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.dependencies import (
    get_dataset_deletion_service,
    get_dataset_query_service,
    get_dataset_registration_service,
    get_dataset_update_service,
)
from app.exceptions import (
    DatasetNotFoundError,
    DatasetVersionNotFoundError,
    InvalidArtifactNameError,
    InvalidLineageReferenceError,
    LineageReferenceNotFoundError,
)
from app.pagination import resolve_list_pagination
from app.routes._artifact_list_params import (
    ARTIFACT_LIST_PAGE_DESCRIPTION,
    ARTIFACT_LIST_PAGE_SIZE_DESCRIPTION,
    ARTIFACT_LIST_SEARCH_DESCRIPTION,
)
from app.routes._error_handling import handle_registration_errors
from app.schemas.artifacts import (
    ArtifactListResponse,
    ArtifactSummary,
)
from app.schemas.datasets import (
    GetDatasetMetadataResponse,
    GetDatasetVersionResponse,
    ListDatasetVersionsResponse,
    RegisterDatasetVersionRequest,
    RegisterDatasetVersionResponse,
    UpdateDatasetMetadataRequest,
    UpdateDatasetVersionRequest,
    UpdateDatasetVersionResponse,
)
from app.services.models import (
    DatasetDeletionService,
    DatasetQueryService,
    DatasetRegistrationService,
    DatasetUpdateService,
)

router = APIRouter(prefix="/api/datasets")


@router.get("", status_code=200)
async def list_datasets(
    *,
    search: str | None = Query(None, description=ARTIFACT_LIST_SEARCH_DESCRIPTION),
    limit: int = Query(50, ge=1, le=1000, description="Results per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    page: int | None = Query(None, ge=1, description=ARTIFACT_LIST_PAGE_DESCRIPTION),
    page_size: int | None = Query(
        None, ge=1, le=1000, description=ARTIFACT_LIST_PAGE_SIZE_DESCRIPTION
    ),
    service: Annotated[DatasetQueryService, Depends(get_dataset_query_service)],
) -> ArtifactListResponse[ArtifactSummary]:
    """List all datasets with optional search and pagination.

    Query parameters
    ----------------
    - ``search``: See parameter description (wildcards and optional JSON containment).
    - ``limit`` / ``offset``: Offset-based pagination (defaults: 50 / 0).
    - ``page`` / ``page_size``: Page-based pagination; when ``page`` is set,
      ``limit`` and ``offset`` are ignored.

    Returns ``total`` (match count) and ``items`` (artifact summaries).
    """
    lim, off = resolve_list_pagination(
        limit=limit, offset=offset, page=page, page_size=page_size
    )
    return await service.list_datasets(search=search, limit=lim, offset=off)


@router.post("/{name}/versions", status_code=201)
@handle_registration_errors
async def register_dataset_version(
    name: str,
    request: RegisterDatasetVersionRequest,
    service: Annotated[
        DatasetRegistrationService, Depends(get_dataset_registration_service)
    ],
) -> RegisterDatasetVersionResponse:
    return await service.register_dataset_version(name, request)


@router.get("/{name}/versions", status_code=200)
async def list_dataset_versions(
    name: str,
    service: Annotated[DatasetQueryService, Depends(get_dataset_query_service)],
) -> ListDatasetVersionsResponse:
    try:
        return await service.list_dataset_versions(name=name)
    except InvalidArtifactNameError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    except DatasetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail)


@router.get("/{name}/versions/{version}", status_code=200)
async def get_dataset_version(
    name: str,
    version: str,
    service: Annotated[DatasetQueryService, Depends(get_dataset_query_service)],
) -> GetDatasetVersionResponse:
    try:
        return await service.get_dataset_version(name=name, version=version)
    except InvalidArtifactNameError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    except (DatasetNotFoundError, DatasetVersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=exc.detail)


@router.get("/{name}", status_code=200)
async def get_dataset_metadata(
    name: str,
    service: Annotated[DatasetQueryService, Depends(get_dataset_query_service)],
) -> GetDatasetMetadataResponse:
    try:
        return await service.get_dataset_metadata(name=name)
    except InvalidArtifactNameError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    except DatasetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail)


@router.put("/{name}", status_code=200)
async def update_dataset_metadata(
    name: str,
    request: UpdateDatasetMetadataRequest,
    service: Annotated[DatasetUpdateService, Depends(get_dataset_update_service)],
) -> GetDatasetMetadataResponse:
    try:
        return await service.update_dataset_metadata(name=name, request=request)
    except (InvalidArtifactNameError, InvalidLineageReferenceError) as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    except (DatasetNotFoundError, LineageReferenceNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=exc.detail)


@router.patch("/{name}/versions/{version}", status_code=200)
async def update_dataset_version(
    name: str,
    version: str,
    request: UpdateDatasetVersionRequest,
    service: Annotated[DatasetUpdateService, Depends(get_dataset_update_service)],
) -> UpdateDatasetVersionResponse:
    try:
        return await service.update_dataset_version(
            name=name, version=version, request=request
        )
    except InvalidArtifactNameError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    except (DatasetNotFoundError, DatasetVersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=exc.detail)


@router.delete("/{name}/versions/{version}", status_code=204)
async def delete_dataset_version(
    name: str,
    version: str,
    service: Annotated[DatasetDeletionService, Depends(get_dataset_deletion_service)],
) -> Response:
    try:
        await service.delete_dataset_version(name=name, version=version)
    except InvalidArtifactNameError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    except (DatasetNotFoundError, DatasetVersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=exc.detail)
    return Response(status_code=204)
