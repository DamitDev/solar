"""Model-version routes under /api/models."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response

from app.dependencies import (
    get_model_deletion_service,
    get_model_query_service,
    get_model_registration_service,
)
from app.exceptions import (
    InvalidArtifactNameError,
    ModelNotFoundError,
    ModelVersionNotFoundError,
)
from app.routes._error_handling import handle_registration_errors
from app.schemas.models import (
    GetModelVersionResponse,
    ListModelVersionsResponse,
    RegisterModelVersionRequest,
    RegisterModelVersionResponse,
)
from app.services.models import (
    ModelDeletionService,
    ModelQueryService,
    ModelRegistrationService,
)

router = APIRouter(prefix="/api/models")


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
