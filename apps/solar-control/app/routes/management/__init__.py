from fastapi import APIRouter

from app.jobs import router as jobs_router

from . import (
    api_keys,
    catalog,
    endpoints,
    gateway,
    hosts,
    instances,
    intents,
    models,
    pulls,
    reservations,
    resources,
    storage,
    uploads,
)

router = APIRouter(prefix="/api", tags=["management"])
router.include_router(hosts.router)
router.include_router(endpoints.router)
router.include_router(api_keys.router)
router.include_router(gateway.router)
router.include_router(models.router)
router.include_router(jobs_router.router)
router.include_router(resources.router)
router.include_router(instances.router)
router.include_router(reservations.router)
router.include_router(intents.router)
router.include_router(catalog.router)
router.include_router(storage.router)
router.include_router(uploads.router)
router.include_router(pulls.router)
