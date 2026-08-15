"""API key management routes (flat /api/api-keys)."""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, model_validator

from app.auth import invalidate_endpoint_cache
from app.database.api_keys import api_key_db
from app.database.endpoints import endpoint_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


class ApiKeyCreate(BaseModel):
    endpoint_id: str
    name: str
    description: str | None = None


class ApiKeyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    enabled: bool | None = None
    endpoint_id: str | None = None
    _description_provided: bool = False

    @model_validator(mode="before")
    @classmethod
    def _track_description(cls, values: dict) -> dict:
        if isinstance(values, dict) and "description" in values:
            values["_description_provided"] = True
        return values


async def _emit_api_keys_update() -> None:
    """Emit the full API key list on /webui after any key mutation."""
    try:
        from app.socketio_app import sio

        keys = await api_key_db.list_all()
        await sio.emit(
            "api_keys_update",
            {"api_keys": [key.model_dump() for key in keys]},
            namespace="/webui",
        )
    except Exception:
        logger.warning("Failed to emit api_keys_update", exc_info=True)


@router.get("")
async def list_api_keys(endpoint_id: str | None = None):
    if endpoint_id:
        ep = await endpoint_db.get_endpoint(endpoint_id)
        if not ep:
            raise HTTPException(status_code=404, detail="Endpoint not found")
        keys = await api_key_db.list_for_endpoint(endpoint_id)
    else:
        keys = await api_key_db.list_all()
    return [key.model_dump() for key in keys]


@router.post("")
async def create_api_key(data: ApiKeyCreate):
    ep = await endpoint_db.get_endpoint(data.endpoint_id)
    if not ep:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    try:
        key = await api_key_db.create(
            endpoint_id=data.endpoint_id,
            name=data.name,
            description=data.description,
        )
    except Exception as e:  # noqa: BLE001
        if "unique" in str(e).lower():
            raise HTTPException(
                status_code=409,
                detail="Key name already exists for this endpoint",
            )
        raise HTTPException(status_code=500, detail=str(e))
    await invalidate_endpoint_cache()
    await _emit_api_keys_update()
    return key.model_dump()


@router.get("/{key_id}")
async def get_api_key(key_id: str):
    key = await api_key_db.get(key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    return key.model_dump()


@router.put("/{key_id}")
async def update_api_key(key_id: str, data: ApiKeyUpdate):
    kwargs: dict = {}
    if data.name is not None:
        kwargs["name"] = data.name
    if data._description_provided:
        kwargs["description"] = data.description
    if data.endpoint_id is not None:
        ep = await endpoint_db.get_endpoint(data.endpoint_id)
        if not ep:
            raise HTTPException(status_code=404, detail="Endpoint not found")
        kwargs["endpoint_id"] = data.endpoint_id
    if data.enabled is not None:
        kwargs["enabled"] = data.enabled

    if not kwargs:
        key = await api_key_db.get(key_id)
    else:
        try:
            key = await api_key_db.update(key_id, **kwargs)
        except Exception as e:  # noqa: BLE001
            if "unique" in str(e).lower():
                raise HTTPException(
                    status_code=409,
                    detail="Key name already exists for this endpoint",
                )
            raise HTTPException(status_code=500, detail=str(e))
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    await invalidate_endpoint_cache()
    await _emit_api_keys_update()
    return key.model_dump()


@router.post("/{key_id}/rotate")
async def rotate_api_key(key_id: str):
    key = await api_key_db.rotate(key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    await invalidate_endpoint_cache()
    await _emit_api_keys_update()
    return key.model_dump()


@router.delete("/{key_id}")
async def delete_api_key(key_id: str):
    key = await api_key_db.get(key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    await api_key_db.delete(key_id)
    await invalidate_endpoint_cache()
    await _emit_api_keys_update()
    return {"message": f"API key '{key.name}' deleted", "id": key_id}
