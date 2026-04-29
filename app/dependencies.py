"""Application-level FastAPI dependency providers.

These thin callables are designed for use with :func:`fastapi.Depends` and live in a
single stable module so any route or test can import them without coupling to the
internal wiring of individual subsystems.

Providers
---------
get_harbor_client
    Returns the app-level :class:`~harbor_oci_client.HarborClient` singleton
    initialised during application startup.

get_model_registration_service / get_dataset_registration_service
    Build registration services per request: :class:`~sqlalchemy.ext.asyncio.AsyncSession`
    plus Harbor client for pre-flight artifact verification.

get_model_query_service / get_dataset_query_service
    Build read-only query services (session only): version lookup, catalog lists,
    artifact-level metadata GET.

get_model_update_service / get_dataset_update_service
    Build metadata / version JSONB update services (session only): PUT artifact
    metadata, PATCH version metadata — PostgreSQL only, no Harbor.

get_model_deletion_service / get_dataset_deletion_service
    Build deletion services (session only): remove a single version row.

Usage::

    from typing import Annotated
    from fastapi import Depends
    from app.dependencies import get_model_registration_service
    from app.services.models import ModelRegistrationService

    @router.post("/{name}/versions", status_code=201)
    async def register_model_version(
        name: str,
        request: RegisterModelVersionRequest,
        service: Annotated[ModelRegistrationService, Depends(get_model_registration_service)],
    ) -> RegisterModelVersionResponse:
        return await service.register_model_version(name, request)
"""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db_session
from app.harbor import HarborClient, harbor_client
from app.services.models import (
    DatasetDeletionService,
    DatasetQueryService,
    DatasetUpdateService,
    DatasetRegistrationService,
    ModelDeletionService,
    ModelQueryService,
    ModelRegistrationService,
    ModelUpdateService,
)


def get_harbor_client() -> HarborClient:
    """Return the app-level :class:`~harbor_oci_client.HarborClient` singleton.

    The singleton is initialised in the application lifespan (``init_harbor``);
    this provider simply surfaces it for injection into FastAPI route signatures.

    Raises
    ------
    RuntimeError
        If called before the lifespan has run ``init_harbor()``.
    """
    return harbor_client()


def get_model_registration_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    harbor: Annotated[HarborClient, Depends(get_harbor_client)],
) -> ModelRegistrationService:
    """Construct a :class:`~app.services.models.ModelRegistrationService` per request.

    Wires together the per-request :class:`~sqlalchemy.ext.asyncio.AsyncSession`
    (transaction boundary owned by :func:`~app.database.get_db_session`) and the
    app-level :class:`~harbor_oci_client.HarborClient` singleton.
    """
    return ModelRegistrationService(harbor=harbor, session=session)


def get_model_query_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ModelQueryService:
    """Construct a :class:`~app.services.models.ModelQueryService` per request."""
    return ModelQueryService(session=session)


def get_model_update_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ModelUpdateService:
    """Construct a :class:`~app.services.models.ModelUpdateService` per request.

    Wires the per-request :class:`~sqlalchemy.ext.asyncio.AsyncSession`.
    """
    return ModelUpdateService(session=session)


def get_model_deletion_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ModelDeletionService:
    """Construct a :class:`~app.services.models.ModelDeletionService` per request."""
    return ModelDeletionService(session=session)


def get_dataset_registration_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    harbor: Annotated[HarborClient, Depends(get_harbor_client)],
) -> DatasetRegistrationService:
    """Construct a :class:`~app.services.models.DatasetRegistrationService` per request.

    Wires together the per-request :class:`~sqlalchemy.ext.asyncio.AsyncSession`
    and the app-level :class:`~harbor_oci_client.HarborClient` singleton.
    """
    return DatasetRegistrationService(harbor=harbor, session=session)


def get_dataset_query_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DatasetQueryService:
    """Construct a :class:`~app.services.models.DatasetQueryService` per request."""
    return DatasetQueryService(session=session)


def get_dataset_update_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DatasetUpdateService:
    """Construct a :class:`~app.services.models.DatasetUpdateService` per request."""
    return DatasetUpdateService(session=session)


def get_dataset_deletion_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> DatasetDeletionService:
    """Construct a :class:`~app.services.models.DatasetDeletionService` per request."""
    return DatasetDeletionService(session=session)
