"""Public status summary for the Solar Status SPA.

GET /api/status — current availability and 90-day uptime per virtual model,
alias and non-managed instance. Public and unauthenticated (see the narrow
exemption in ``app.auth``); every other /api route needs the management key.
"""

from fastapi import APIRouter

from app.models.status import StatusSummary
from app.services.status_summary import status_summary_service

router = APIRouter(prefix="/status", tags=["status"])


@router.get("", response_model=StatusSummary)
async def get_status() -> StatusSummary:
    """Return the status summary."""
    return await status_summary_service.build()
