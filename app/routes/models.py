"""Model-version routes under /api/models."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_model_query_service, get_model_registration_service
from app.exceptions import (
    ArtifactCategoryConflictError,
    ArtifactNotFoundInHarborError,
    HarborVerificationError,
    InvalidArtifactNameError,
    ModelNotFoundError,
    ModelVersionNotFoundError,
    VersionAlreadyExistsError,
)
from app.schemas.models import (
    GetModelVersionResponse,
    RegisterModelVersionRequest,
    RegisterModelVersionResponse,
)
from app.services.models import ModelQueryService, ModelRegistrationService

router = APIRouter(prefix="/api/models")


@router.post("/{name}/versions", status_code=201)
async def register_model_version(
    name: str,
    request: RegisterModelVersionRequest,
    service: Annotated[
        ModelRegistrationService, Depends(get_model_registration_service)
    ],
) -> RegisterModelVersionResponse:
    try:
        return await service.register_model_version(name, request)
    except InvalidArtifactNameError as exc:
        raise HTTPException(status_code=422, detail=exc.detail)
    except ArtifactNotFoundInHarborError as exc:
        raise HTTPException(status_code=404, detail=exc.detail)
    except HarborVerificationError as exc:
        raise HTTPException(status_code=502, detail=exc.detail)
    except (ArtifactCategoryConflictError, VersionAlreadyExistsError) as exc:
        raise HTTPException(status_code=409, detail=exc.detail)


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
