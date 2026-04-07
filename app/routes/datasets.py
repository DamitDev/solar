"""POST /api/datasets/{name}/versions — register a Harbor OCI artifact as a dataset version."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.dependencies import get_dataset_registration_service
from app.routes._error_handling import handle_registration_errors
from app.schemas.datasets import (
    RegisterDatasetVersionRequest,
    RegisterDatasetVersionResponse,
)
from app.services.models import DatasetRegistrationService

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
