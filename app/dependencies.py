"""Application-level FastAPI dependency providers.

These thin callables are designed for use with :func:`fastapi.Depends` and live in a
single stable module so any route or test can import them without coupling to the
internal wiring of individual subsystems.

Providers
---------
get_harbor_client
    Returns the app-level :class:`~harbor_oci_client.HarborClient` singleton
    initialised during application startup.

get_model_registration_service
    Builds a :class:`~app.services.models.ModelRegistrationService` per request,
    wiring together the per-request :class:`~sqlalchemy.ext.asyncio.AsyncSession`
    (from :func:`app.database.get_db_session`) and the Harbor singleton
    (from :func:`get_harbor_client`).

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
from app.services.models import ModelRegistrationService


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
