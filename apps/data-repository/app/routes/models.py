"""Model-version routes under /api/models."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.dependencies import (
    get_model_deletion_service,
    get_model_query_service,
    get_model_registration_service,
    get_model_update_service,
)
from app.exceptions import (
    InvalidArtifactNameError,
    InvalidLineageReferenceError,
    LineageReferenceNotFoundError,
    ModelNotFoundError,
    ModelVersionNotFoundError,
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
from app.schemas.models import (
    GetModelMetadataResponse,
    GetModelVersionResponse,
    ListModelVersionsResponse,
    RegisterModelVersionRequest,
    RegisterModelVersionResponse,
    UpdateModelMetadataRequest,
    UpdateModelVersionRequest,
    UpdateModelVersionResponse,
)
from app.services.models import (
    ModelDeletionService,
    ModelQueryService,
    ModelRegistrationService,
    ModelUpdateService,
)

router = APIRouter(prefix="/api/models")


@router.get("", status_code=200)
async def list_models(
    *,
    search: str | None = Query(None, description=ARTIFACT_LIST_SEARCH_DESCRIPTION),
    limit: int = Query(50, ge=1, le=1000, description="Results per page"),
    offset: int = Query(0, ge=0, description="Pagination offset"),
    page: int | None = Query(None, ge=1, description=ARTIFACT_LIST_PAGE_DESCRIPTION),
    page_size: int | None = Query(
        None, ge=1, le=1000, description=ARTIFACT_LIST_PAGE_SIZE_DESCRIPTION
    ),
    service: Annotated[ModelQueryService, Depends(get_model_query_service)],
) -> ArtifactListResponse[ArtifactSummary]:
    """List all models with optional search and pagination.

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
    return await service.list_models(search=search, limit=lim, offset=off)


@router.post("/{name}/versions", status_code=201)
@handle_registration_errors
async def register_model_version(
    name: str,
    request: RegisterModelVersionRequest,
    service: Annotated[
        ModelRegistrationService, Depends(get_model_registration_service)
    ],
) -> RegisterModelVersionResponse:
    return await service.register_model_version(name, request)


@router.get("/{name}/versions", status_code=200)
async def list_model_versions(
    name: str,
    service: Annotated[ModelQueryService, Depends(get_model_query_service)],
) -> ListModelVersionsResponse:
    try:
        return await service.list_model_versions(name=name)
    except InvalidArtifactNameError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    except ModelNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail)


@router.get("/{name}/versions/{version}", status_code=200)
async def get_model_version(
    name: str,
    version: str,
    service: Annotated[ModelQueryService, Depends(get_model_query_service)],
) -> GetModelVersionResponse:
    try:
        return await service.get_model_version(name=name, version=version)
    except InvalidArtifactNameError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    except (ModelNotFoundError, ModelVersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=exc.detail)


@router.get("/{name}", status_code=200)
async def get_model_metadata(
    name: str,
    service: Annotated[ModelQueryService, Depends(get_model_query_service)],
) -> GetModelMetadataResponse:
    try:
        return await service.get_model_metadata(name=name)
    except InvalidArtifactNameError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    except ModelNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail)


@router.put("/{name}", status_code=200)
async def update_model_metadata(
    name: str,
    request: UpdateModelMetadataRequest,
    service: Annotated[ModelUpdateService, Depends(get_model_update_service)],
) -> GetModelMetadataResponse:
    try:
        return await service.update_model_metadata(name=name, request=request)
    except (InvalidArtifactNameError, InvalidLineageReferenceError) as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    except (ModelNotFoundError, LineageReferenceNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=exc.detail)


@router.patch("/{name}/versions/{version}", status_code=200)
async def update_model_version(
    name: str,
    version: str,
    request: UpdateModelVersionRequest,
    service: Annotated[ModelUpdateService, Depends(get_model_update_service)],
) -> UpdateModelVersionResponse:
    try:
        return await service.update_model_version(
            name=name, version=version, request=request
        )
    except InvalidArtifactNameError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    except (ModelNotFoundError, ModelVersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=exc.detail)


@router.delete("/{name}/versions/{version}", status_code=204)
async def delete_model_version(
    name: str,
    version: str,
    service: Annotated[ModelDeletionService, Depends(get_model_deletion_service)],
) -> Response:
    try:
        await service.delete_model_version(name=name, version=version)
    except InvalidArtifactNameError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    except (ModelNotFoundError, ModelVersionNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=exc.detail)
    return Response(status_code=204)


@router.delete("/{name}", status_code=204)
async def delete_model(
    name: str,
    service: Annotated[ModelDeletionService, Depends(get_model_deletion_service)],
) -> Response:
    """Delete the whole model artifact; version rows cascade.

    Pure unregister — Harbor cleanup is orchestrated by Solar Control's
    catalog delete relay (S-048) before this endpoint is called.
    """
    try:
        await service.delete_model(name=name)
    except InvalidArtifactNameError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    except ModelNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.detail)
    return Response(status_code=204)
