"""Virtual model API routes (S-060).

POST   /api/virtual-models          — create a virtual model
GET    /api/virtual-models          — list virtual models (with live target status)
PUT    /api/virtual-models/{name}   — update targets/contract/description
DELETE /api/virtual-models/{name}   — delete a virtual model
"""

import logging

from fastapi import APIRouter, HTTPException

from app.database.virtual_models import virtual_model_db
from app.models.virtual_model import (
    VirtualModelCreate,
    VirtualModelResponse,
    VirtualModelUpdate,
)
from app.redis_state import registry_store
from app.services.virtual_model_cache import virtual_model_cache
from app.services.virtual_model_contract import contract_violation
from app.services.virtual_model_validation import (
    validate_virtual_model,
    validation_errors_to_detail,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/virtual-models", tags=["virtual-models"])


async def _live_registry() -> dict[str, list]:
    return await registry_store.get_registry()


def _enrich(
    vm: VirtualModelResponse, registry: dict[str, list]
) -> VirtualModelResponse:
    """Fill in target_status for one virtual model against the live registry."""
    status: dict[str, str] = {}
    for target in vm.targets:
        instances = registry.get(target)
        if not instances:
            status[target] = "missing"
            continue
        reasons = [
            reason
            for inst in instances
            if (
                reason := contract_violation(
                    getattr(inst, "context_size", None),
                    getattr(inst, "capabilities", None),
                    vm.contract,
                )
            )
            is not None
        ]
        if len(reasons) == len(instances):
            reason = reasons[0]
            if "unknown context size" in reason:
                # The contract cannot be verified against this target (host
                # predates the context probe): serve-but-flag, never block.
                status[target] = "unverified"
            else:
                status[target] = f"violating: {reason}"
        else:
            status[target] = "satisfied"
    return vm.model_copy(update={"target_status": status})


async def _validated_create(data: VirtualModelCreate) -> list[str]:
    """Run save-time validation; raise 422 on hard errors, return warnings."""
    errors, warnings = validate_virtual_model(data, await _live_registry())
    if errors:
        raise HTTPException(status_code=422, detail=validation_errors_to_detail(errors))
    return warnings


@router.get("", response_model=list[VirtualModelResponse])
async def list_virtual_models() -> list[VirtualModelResponse]:
    """List virtual models; definitions are TTL-cached, target status is live."""
    entries = virtual_model_cache.get_all()
    if entries is None:
        entries = await virtual_model_db.list_all()
        virtual_model_cache.set_all(entries)
    registry = await _live_registry()
    return [_enrich(vm, registry) for vm in entries]


@router.post("", response_model=VirtualModelResponse, status_code=201)
async def create_virtual_model(body: VirtualModelCreate) -> VirtualModelResponse:
    warnings = await _validated_create(body)
    try:
        created = await virtual_model_db.create(body)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    virtual_model_cache.invalidate()
    return created.model_copy(update={"warnings": warnings})


@router.put("/{name}", response_model=VirtualModelResponse)
async def update_virtual_model(
    name: str, body: VirtualModelUpdate
) -> VirtualModelResponse:
    existing = await virtual_model_db.get_by_name(name)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Virtual model '{name}' not found")

    merged = VirtualModelCreate(
        name=name,
        targets=body.targets if body.targets is not None else existing.targets,
        description=(
            body.description if body.description is not None else existing.description
        ),
        contract=(body.contract if body.contract is not None else existing.contract),
    )
    await _validated_create(merged)

    updated = await virtual_model_db.update(name, body)
    if not updated:  # pragma: no cover - raced delete
        raise HTTPException(status_code=404, detail=f"Virtual model '{name}' not found")
    virtual_model_cache.invalidate()
    return updated


@router.delete("/{name}")
async def delete_virtual_model(name: str) -> dict:
    deleted = await virtual_model_db.delete(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Virtual model '{name}' not found")
    virtual_model_cache.invalidate()
    return {"message": f"Virtual model '{name}' deleted"}
