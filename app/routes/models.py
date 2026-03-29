"""POST /api/models/{name}/versions — register a Harbor OCI artifact as a model version."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import get_model_registration_service
from app.exceptions import (
    ArtifactCategoryConflictError,
    ArtifactNotFoundInHarborError,
    HarborVerificationError,
    InvalidArtifactNameError,
    VersionAlreadyExistsError,
)
from app.schemas.models import RegisterModelVersionRequest, RegisterModelVersionResponse
from app.services.models import ModelRegistrationService

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
