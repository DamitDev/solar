"""Intent API routes (S-040).

POST   /api/intents          — submit a deployment intent
GET    /api/intents          — list active intents
GET    /api/intents/{id}     — get a single intent
PUT    /api/intents/{id}     — replace an intent's spec (S-044)
DELETE /api/intents/{id}     — delete an intent (soft-delete)
"""

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from app.database.intents import intent_db
from app.models.intent import (
    IntentCreate,
    IntentDeletedResponse,
    IntentPhase,
    IntentResponse,
    IntentUpdate,
)
from app.validation import (
    canonicalize_intent_backend,
    validate_intent_create,
    validate_intent_update,
    validate_intent_warnings,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/intents", tags=["intents"])


@router.post("", response_model=IntentResponse, status_code=201)
async def create_intent(request: Request, body: IntentCreate) -> IntentResponse:
    """Submit a desired-state deployment intent (S-039 §12.1).

    The intent is stored with phase='pending' and will be reconciled
    once S-041 reconciliation ships.
    """
    data = body.model_dump()

    # Validate
    errors = validate_intent_create(data)
    if errors:
        raise HTTPException(
            status_code=422,
            detail={"detail": "Invalid intent", "errors": errors},
        )

    # Canonicalize backend values (C1): store chat_template_kwargs in the
    # same compact canonical form the host produces, so new intents never
    # start out looking drifted. Raises 422 on malformed JSON.
    canonicalize_intent_backend(data["backend"])

    # C3: fleet-aware validation — hard errors join the 422, advisory
    # warnings ride along on the success response.
    from app.services.intent_validation import validate_intent_fleet

    hard_errors, warnings = await validate_intent_fleet(data)
    if hard_errors:
        raise HTTPException(
            status_code=422,
            detail={"detail": "Invalid intent", "errors": errors + hard_errors},
        )
    warnings = warnings + validate_intent_warnings(data)

    # Check alias conflict
    conflict = await intent_db.check_alias_conflict(data["alias"])
    if conflict:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": f"An active intent already exists for alias '{data['alias']}'"
            },
        )

    intent = await intent_db.create_intent(
        alias=data["alias"],
        model_source=data["model_source"],
        replicas=data.get("replicas", 1),
        priority=data.get("priority", "production"),
        strategy=data.get("strategy", "rolling"),
        backend=data["backend"],
        placement=data.get("placement", {}),
        resources=data.get("resources", {}),
        metadata=data.get("metadata", {}),
    )

    logger.info("Intent created: id=%s alias=%s", intent.id, intent.alias)
    # Wake reconciler to process the new intent immediately
    from app.services.reconciliation import reconciler

    reconciler.wake()
    # C3: advisory warnings are response-only — never persisted.
    response = intent.model_copy(update={"warnings": warnings or None})
    return response


@router.get("", response_model=list[IntentResponse])
async def list_intents(
    request: Request,
    alias: str | None = Query(None),
    priority: str | None = Query(None),
    phase: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[IntentResponse]:
    """List active intents with optional filters (S-039 §12.2)."""
    return await intent_db.list_intents(
        alias=alias,
        priority=priority,
        phase=phase,
        limit=limit,
        offset=offset,
    )


@router.get("/{intent_id}", response_model=IntentResponse)
async def get_intent(request: Request, intent_id: str) -> IntentResponse:
    """Get a single intent by ID (S-039 §12.3)."""
    intent = await intent_db.get_intent(intent_id)
    if intent is None:
        raise HTTPException(status_code=404, detail="Intent not found")
    return intent


@router.put("/{intent_id}", response_model=IntentResponse)
async def update_intent(
    request: Request, intent_id: str, body: IntentUpdate
) -> IntentResponse:
    """Replace an intent's spec (S-039 §12.5).

    Full-replace semantics: the request carries the complete spec, and a
    field left out is reset to its default exactly as on create. The
    reconciler converges the change under the strategy in the updated spec.
    """
    existing = await intent_db.get_intent(intent_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Intent not found")
    if existing.status.phase == IntentPhase.DELETING:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": (
                    f"Intent '{existing.alias}' is being deleted and cannot be "
                    f"updated"
                )
            },
        )

    data = body.model_dump()

    errors = validate_intent_update(data, current_alias=existing.alias)
    if errors:
        raise HTTPException(
            status_code=422,
            detail={"detail": "Invalid intent", "errors": errors},
        )

    # Canonicalize backend values (C1) — see create_intent.
    canonicalize_intent_backend(data["backend"])

    # C3: fleet-aware validation — see create_intent.
    from app.services.intent_validation import validate_intent_fleet

    hard_errors, warnings = await validate_intent_fleet(data)
    if hard_errors:
        raise HTTPException(
            status_code=422,
            detail={"detail": "Invalid intent", "errors": errors + hard_errors},
        )
    warnings = warnings + validate_intent_warnings(data)

    conflict = await intent_db.check_alias_conflict(
        existing.alias, exclude_id=intent_id
    )
    if conflict:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": (
                    f"An active intent already exists for alias " f"'{existing.alias}'"
                )
            },
        )

    intent = await intent_db.update_intent(
        intent_id,
        model_source=data["model_source"],
        replicas=data.get("replicas", 1),
        priority=data.get("priority", "production"),
        strategy=data.get("strategy", "rolling"),
        backend=data["backend"],
        placement=data.get("placement", {}),
        resources=data.get("resources", {}),
        metadata=data.get("metadata", {}),
    )
    if intent is None:
        raise HTTPException(status_code=404, detail="Intent not found")

    logger.info("Intent updated: id=%s alias=%s", intent.id, intent.alias)
    # Wake the reconciler so the change is applied now, not on the next tick
    from app.services.reconciliation import reconciler

    reconciler.wake()
    # C3: advisory warnings are response-only — never persisted.
    response = intent.model_copy(update={"warnings": warnings or None})
    return response


@router.delete(
    "/{intent_id}",
    response_model=IntentDeletedResponse,
    status_code=202,
)
async def delete_intent(
    request: Request,
    intent_id: str,
    orphan: bool = Query(False),
) -> IntentDeletedResponse:
    """Delete an intent (S-039 §12.4).

    Marks the intent as 'deleting'. The S-041 reconciler will stop
    managed instances (or orphan them if ?orphan=true).
    """
    intent = await intent_db.soft_delete_intent(intent_id, orphan=orphan)
    if intent is None:
        raise HTTPException(status_code=404, detail="Intent not found")

    logger.info(
        "Intent deleted: id=%s alias=%s orphan=%s", intent_id, intent.alias, orphan
    )
    # Wake reconciler to process the deletion immediately
    from app.services.reconciliation import reconciler

    reconciler.wake()
    return IntentDeletedResponse(
        id=intent.id,
        alias=intent.alias,
        phase=IntentPhase.DELETING,
        message=(
            "Intent deletion initiated. Managed instances will be orphaned."
            if orphan
            else "Intent deletion initiated. Managed instances will be stopped."
        ),
    )
