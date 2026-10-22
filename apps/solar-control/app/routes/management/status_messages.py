"""Status message management routes.

POST   /api/status-messages        — create a message
GET    /api/status-messages        — list all messages
PUT    /api/status-messages/{id}   — update body/type/window
DELETE /api/status-messages/{id}   — delete a message
"""

from uuid import UUID

from fastapi import APIRouter, HTTPException

from app.database.status_messages import (
    StatusMessageWindowError,
    status_message_db,
)
from app.models.status_message import (
    StatusMessageCreate,
    StatusMessageResponse,
    StatusMessageUpdate,
)

router = APIRouter(prefix="/status-messages", tags=["status-messages"])


def _not_found(message_id: str) -> HTTPException:
    return HTTPException(
        status_code=404, detail=f"Status message '{message_id}' not found"
    )


def _require_uuid(message_id: str) -> None:
    """Unknown-but-malformed ids are plain 404s, never database errors."""
    try:
        UUID(message_id)
    except ValueError as exc:
        raise _not_found(message_id) from exc


@router.get("", response_model=list[StatusMessageResponse])
async def list_status_messages() -> list[StatusMessageResponse]:
    return await status_message_db.list_all()


@router.post("", response_model=StatusMessageResponse, status_code=201)
async def create_status_message(body: StatusMessageCreate) -> StatusMessageResponse:
    return await status_message_db.create(body)


@router.put("/{message_id}", response_model=StatusMessageResponse)
async def update_status_message(
    message_id: str, body: StatusMessageUpdate
) -> StatusMessageResponse:
    _require_uuid(message_id)
    # The database merges the stored window under a row lock and rejects an
    # inverted result inside the write's transaction, so a concurrent partial
    # update cannot validate against a stale window.
    try:
        updated = await status_message_db.update(message_id, body)
    except StatusMessageWindowError as exc:
        raise HTTPException(
            status_code=422, detail="start_time must not be after end_time"
        ) from exc
    if not updated:
        raise _not_found(message_id)
    return updated


@router.delete("/{message_id}")
async def delete_status_message(message_id: str) -> dict:
    _require_uuid(message_id)
    if not await status_message_db.delete(message_id):
        raise _not_found(message_id)
    return {"message": f"Status message '{message_id}' deleted"}
