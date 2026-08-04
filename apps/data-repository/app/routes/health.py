import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.database import get_session_factory

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
async def health_check():
    try:
        factory = get_session_factory()
        async with factory() as session:
            await session.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as e:
        logger.error("Health check failed: %s", e)
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "service": "data-repository",
                "database": "error",
                "detail": str(e),
            },
        )

    return {"status": "healthy", "service": "data-repository", "database": db_status}
