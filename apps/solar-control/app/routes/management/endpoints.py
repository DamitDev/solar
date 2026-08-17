"""API endpoint management routes (under /api/endpoints)."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.auth import invalidate_endpoint_cache
from app.database.api_keys import api_key_db
from app.database.endpoints import endpoint_db
from app.redis_state import registry_store
from app.routes.management.api_keys import _emit_api_keys_update
from app.services.model_access import filter_aliases, filter_aliases_for_patterns

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/endpoints", tags=["endpoints"])


class EndpointCreate(BaseModel):
    name: str
    description: str | None = None
    serve_all_models: bool = True
    model_patterns: list[str] = []


class EndpointUpdate(BaseModel):
    """Partial update. Absent keys are left untouched.

    Presence is read from ``model_fields_set`` so that an explicit ``null``
    ``description`` clears the column while omitting the key preserves it.
    """

    name: str | None = None
    description: str | None = None
    serve_all_models: bool | None = None
    model_patterns: list[str] | None = None


class EndpointModelPreview(BaseModel):
    serve_all_models: bool = True
    model_patterns: list[str] = []


async def _emit_endpoints_update() -> None:
    """Emit the current endpoint list on /webui (C5).

    Endpoint records change only when a user edits them, so the RoutingGraph
    should be event-driven and poll only as a disconnected fallback.
    """
    try:
        from app.socketio_app import sio

        endpoints = await endpoint_db.get_all_endpoints()
        await sio.emit(
            "endpoints_update",
            {"endpoints": [ep.model_dump() for ep in endpoints]},
            namespace="/webui",
        )
    except Exception:
        logger.warning("Failed to emit endpoints_update", exc_info=True)


def _serialize(ep, key_count: int) -> dict:
    data = ep.model_dump()
    data["key_count"] = key_count
    return data


async def _list_keys_by_endpoint() -> dict[str, int]:
    keys = await api_key_db.list_all()
    counts: dict[str, int] = {}
    for key in keys:
        counts[key.endpoint_id] = counts.get(key.endpoint_id, 0) + 1
    return counts


async def _registry_aliases() -> list[str]:
    registry = await registry_store.get_registry()
    return [alias for alias, instances in registry.items() if instances]


@router.get("")
async def list_endpoints():
    endpoints = await endpoint_db.get_all_endpoints()
    counts = await _list_keys_by_endpoint()
    return [_serialize(ep, counts.get(ep.id, 0)) for ep in endpoints]


@router.post("")
async def create_endpoint(data: EndpointCreate):
    try:
        ep = await endpoint_db.create_endpoint(
            name=data.name,
            description=data.description,
            serve_all_models=data.serve_all_models,
            model_patterns=data.model_patterns,
        )
        await invalidate_endpoint_cache()
        await _emit_endpoints_update()
        return _serialize(ep, 0)
    except Exception as e:  # noqa: BLE001
        if "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="Endpoint name already exists")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/preview-models")
async def preview_models(data: EndpointModelPreview):
    """Live alias preview for the create/edit form (model scoping).

    ``available`` lists every registry alias so the form can offer a pick list
    instead of making the user guess glob syntax against unseen aliases.
    """
    aliases = await _registry_aliases()
    matched = (
        aliases
        if data.serve_all_models
        else filter_aliases_for_patterns(data.model_patterns, aliases)
    )
    return {"aliases": matched, "count": len(matched), "available": aliases}


@router.get("/{endpoint_id}")
async def get_endpoint(endpoint_id: str):
    ep = await endpoint_db.get_endpoint(endpoint_id)
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    keys = await api_key_db.list_for_endpoint(endpoint_id)
    return _serialize(ep, len(keys))


@router.get("/{endpoint_id}/keys")
async def get_endpoint_keys(endpoint_id: str):
    ep = await endpoint_db.get_endpoint(endpoint_id)
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    keys = await api_key_db.list_for_endpoint(endpoint_id)
    return [key.model_dump() for key in keys]


@router.get("/{endpoint_id}/models")
async def get_endpoint_models(endpoint_id: str):
    ep = await endpoint_db.get_endpoint(endpoint_id)
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    aliases = filter_aliases(ep, await _registry_aliases())
    return {"endpoint": ep.model_dump(), "aliases": aliases, "count": len(aliases)}


@router.put("/{endpoint_id}")
async def update_endpoint(endpoint_id: str, data: EndpointUpdate):
    provided = data.model_fields_set
    kwargs: dict = {}
    if data.name is not None:
        kwargs["name"] = data.name
    if "description" in provided:
        kwargs["description"] = data.description
    if data.serve_all_models is not None:
        kwargs["serve_all_models"] = data.serve_all_models
    if "model_patterns" in provided and data.model_patterns is not None:
        kwargs["model_patterns"] = data.model_patterns

    ep = await endpoint_db.update_endpoint(endpoint_id, **kwargs)
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    await invalidate_endpoint_cache()
    await _emit_endpoints_update()
    keys = await api_key_db.list_for_endpoint(endpoint_id)
    return _serialize(ep, len(keys))


@router.delete("/{endpoint_id}")
async def delete_endpoint(endpoint_id: str):
    ep = await endpoint_db.get_endpoint(endpoint_id)
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    keys = await api_key_db.list_for_endpoint(endpoint_id)
    await endpoint_db.delete_endpoint(endpoint_id)
    await invalidate_endpoint_cache()
    await _emit_endpoints_update()
    await _emit_api_keys_update()
    return {
        "message": f"Endpoint '{ep.name}' deleted",
        "id": endpoint_id,
        "deleted_keys": len(keys),
    }


@router.get("/{endpoint_id}/usage")
async def get_endpoint_usage(endpoint_id: str, hours: int = 24):
    ep = await endpoint_db.get_endpoint(endpoint_id)
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    stats = await endpoint_db.get_usage_stats(endpoint_id, hours=hours)
    return {"endpoint": ep.model_dump(), "hours": hours, "usage": stats}
