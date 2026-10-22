"""Embedded status SPA serving at /status.

Bridges the built solar-status SPA (Vite dist/) into solar-control so
production runs same-origin with control as the only runtime dependency.
"""

from fastapi import APIRouter

from app.config import settings


def create_frontend_router() -> APIRouter:
    """Build the status SPA router.

    The frontend is registered only when the built SPA directory exists at
    startup: without a SPA build, control starts cleanly and /status 404s.
    ``check_dir`` stays at its default instead of being
    disabled — a disabled check on a missing directory would 500 at request
    time instead of 404ing cleanly, and the existence guard above makes the
    startup check a no-op anyway.

    ``fallback="auto"`` serves ``index.html`` for client-side-routed
    navigations while missing assets still 404.
    """
    router = APIRouter()
    if settings.status_dist_dir.is_dir():
        router.frontend("/", directory=settings.status_dist_dir, fallback="auto")
    return router
