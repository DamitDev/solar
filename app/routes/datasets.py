"""Dataset routes under /api/datasets."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.dependencies import (
    get_dataset_query_service,
    get_dataset_registration_service,
)
from app.exceptions import (
    DatasetNotFoundError,
    DatasetVersionNotFoundError,
    InvalidArtifactNameError,
)
from app.routes._error_handling import handle_registration_errors
from app.schemas.datasets import (
    GetDatasetVersionResponse,
    RegisterDatasetVersionRequest,
    RegisterDatasetVersionResponse,
)
from app.services.models import DatasetQueryService, DatasetRegistrationService

router = APIRouter(prefix="/api/datasets")


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
