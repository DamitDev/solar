"""Data Repository — lightweight metadata catalog for OCI artifacts."""

import os
import logging
from contextlib import asynccontextmanager
from importlib.metadata import version as _pkg_version, PackageNotFoundError

from fastapi import FastAPI

from app.config import settings

try:
    __version__ = _pkg_version("data-repository")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"

log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("uvicorn").setLevel(getattr(logging, log_level, logging.INFO))
logging.getLogger("uvicorn.error").setLevel(getattr(logging, log_level, logging.INFO))
logging.getLogger("uvicorn.access").setLevel(getattr(logging, log_level, logging.INFO))


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.database import init_db, close_db
    from app.harbor import init_harbor, close_harbor

    logger.info("Starting Data Repository v%s ...", __version__)

    await init_db(settings.database_url)
    logger.info("PostgreSQL connected")

    await init_harbor(
        settings.harbor_url, settings.harbor_username, settings.harbor_password
    )
    logger.info("Harbor client initialized (%s)", settings.harbor_url)

    logger.info("Data Repository started successfully")

    yield

    logger.info("Shutting down Data Repository...")
    await close_harbor()
    await close_db()
    logger.info("Data Repository shut down")


app = FastAPI(
    title="Data Repository",
    description="Metadata catalog for OCI artifacts stored in Harbor. "
    "Provides registration, catalog, search, and URI resolution.",
    version=__version__,
    lifespan=lifespan,
)

from app.routes.artifacts import router as artifacts_router  # noqa: E402
from app.routes.datasets import router as datasets_router  # noqa: E402
from app.routes.health import router as health_router  # noqa: E402
from app.routes.models import router as models_router  # noqa: E402
from app.routes.resolve import router as resolve_router  # noqa: E402

app.include_router(health_router)
app.include_router(artifacts_router)
app.include_router(models_router)
app.include_router(datasets_router)
app.include_router(resolve_router)


@app.get("/")
async def root():
    return {
        "service": "data-repository",
        "version": __version__,
        "description": "Metadata catalog for models and datasets",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=True)
