"""Host management API routes (under /api/hosts)."""

import asyncio
import logging
import uuid
from typing import Any

import aiohttp
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.config import settings
from app.database.hosts import host_db
from app.models import (
    DrainState,
    Host,
    HostCreate,
    HostDrainStatus,
    HostResponse,
    HostStatus,
)
from app.redis_state import host_store
from app.services import drain
from app.services.migration import create_instance_on_host
from app.validation import validate_host_supports_backend, validate_priority

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hosts", tags=["hosts"])


class PendingApproveRequest(BaseModel):
    name: str
    url: str


@router.get("/pending")
async def list_pending_hosts():
    from app.socketio_app.host_handlers import get_pending_hosts

    return await get_pending_hosts()


@router.post("/pending/{pending_id}/approve", response_model=HostResponse)
async def approve_host(pending_id: str, data: PendingApproveRequest):
    from app.socketio_app.host_handlers import approve_pending_host, get_pending_host

    pending = await get_pending_host(pending_id)
    if not pending:
        raise HTTPException(
            status_code=404, detail="Pending host not found (may have disconnected)"
        )

    host_id = await approve_pending_host(pending_id, data.name, data.url)
    if not host_id:
        raise HTTPException(status_code=404, detail="Pending host not found")

    host = await host_db.get_host(host_id)
    return HostResponse(
        host=host, message=f"Host '{data.name}' approved and registered"
    )


@router.post("/pending/{pending_id}/reject")
async def reject_host(pending_id: str):
    from app.socketio_app.host_handlers import reject_pending_host

    ok = await reject_pending_host(pending_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Pending host not found")
    return {"message": "Host rejected and disconnected"}


@router.post("", response_model=HostResponse)
async def register_host(data: HostCreate):
    """Pre-register a host record. The host does not need to be online."""
    host_id = str(uuid.uuid4())
    host = Host(id=host_id, name=data.name, url=data.url, api_key=data.api_key)
    await host_db.add_host(host)
    return HostResponse(
        host=host, message=f"Host '{data.name}' registered successfully"
    )


@router.get("", response_model=list[Host])
async def list_hosts(role: str | None = Query(None, description="Filter by role")):
    return await host_db.get_all_hosts(role=role)


@router.get("/{host_id}", response_model=Host)
async def get_host(host_id: str):
    host = await host_db.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    return host


@router.delete("/{host_id}", response_model=HostResponse)
async def remove_host(host_id: str):
    host = await host_db.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    await host_db.remove_host(host_id)
    # The instance cache, resource snapshot and pull-progress entries are all
    # keyed by host id with no TTL; nothing else would ever reclaim them.
    try:
        await host_store.purge_host_state(host_id)
    except Exception:
        logger.warning(
            "Failed to purge Redis state for removed host %s", host_id, exc_info=True
        )
    return HostResponse(host=host, message=f"Host '{host.name}' removed successfully")


@router.post("/{host_id}/refresh", response_model=HostResponse)
async def refresh_host_status(host_id: str):
    host = await host_db.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/health"
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status != 200:
                    await host_db.update_host_status(host_id, HostStatus.ERROR)
                    raise HTTPException(
                        status_code=400,
                        detail=f"Health check failed: {response.status}",
                    )

            url = f"{host.url}/instances"
            headers = {"X-API-Key": host.api_key}
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status == 200:
                    await host_db.update_host_status(host_id, HostStatus.ONLINE)
                    host = await host_db.get_host(host_id)
                    return HostResponse(
                        host=host, message=f"Host '{host.name}' is online"
                    )
                else:
                    await host_db.update_host_status(host_id, HostStatus.ERROR)
                    raise HTTPException(
                        status_code=400, detail=f"API auth failed: {response.status}"
                    )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        await host_db.update_host_status(host_id, HostStatus.OFFLINE)
        raise HTTPException(status_code=500, detail=f"Failed to connect: {e!s}")


@router.post("/refresh-all")
async def refresh_all_hosts():
    hosts = await host_db.get_all_hosts()
    results: list[dict[str, str]] = []
    async with aiohttp.ClientSession() as session:
        for host in hosts:
            try:
                url = f"{host.url}/health"
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status != 200:
                        await host_db.update_host_status(host.id, HostStatus.ERROR)
                        results.append(
                            {"host_id": host.id, "name": host.name, "status": "error"}
                        )
                        continue
                url = f"{host.url}/instances"
                headers = {"X-API-Key": host.api_key}
                async with session.get(
                    url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status == 200:
                        await host_db.update_host_status(host.id, HostStatus.ONLINE)
                        results.append(
                            {"host_id": host.id, "name": host.name, "status": "online"}
                        )
                    else:
                        await host_db.update_host_status(host.id, HostStatus.ERROR)
                        results.append(
                            {"host_id": host.id, "name": host.name, "status": "error"}
                        )
            except Exception:  # noqa: BLE001
                await host_db.update_host_status(host.id, HostStatus.OFFLINE)
                results.append(
                    {"host_id": host.id, "name": host.name, "status": "offline"}
                )
    return {"message": f"Refreshed {len(hosts)} hosts", "results": results}


@router.post("/{host_id}/drain", response_model=HostDrainStatus, status_code=202)
async def drain_host(host_id: str):
    """Start draining a host (S-043 §5.1).

    Marks the host ``draining`` after a preflight; the reconciler evacuates
    the intent-managed replicas from there. Idempotent: a host that is
    already draining or drained is returned unchanged.
    """
    host = _require_host(await host_db.get_host(host_id))

    if host.drain_state is not None:
        return await drain.build_drain_status(host)

    blockers = await drain.collect_blockers(host)
    if blockers:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": f"Host '{host.name}' cannot be drained yet",
                "blockers": [b.model_dump() for b in blockers],
            },
        )

    updated = await host_db.set_drain_state(host_id, DrainState.DRAINING)
    host = updated or host
    logger.info("Host '%s' draining requested", host.name)
    await drain.broadcast_drain_state(host)

    # Wake the reconciler so evacuation starts now instead of on the tick.
    from app.services.reconciliation import reconciler

    reconciler.wake()

    return await drain.build_drain_status(host, blockers=[])


@router.delete("/{host_id}/drain", response_model=HostDrainStatus)
async def resume_host(host_id: str):
    """Cancel a drain or return a drained host to service (S-043 §5.2)."""
    host = _require_host(await host_db.get_host(host_id))

    if host.drain_state is None:
        return await drain.build_drain_status(host)

    previous = host.drain_state
    updated = await host_db.set_drain_state(host_id, None)
    host = updated or host
    logger.info("Host '%s' resumed (was %s)", host.name, previous.value)
    await drain.broadcast_drain_state(host)

    # Wake the reconciler: the host is eligible for placement again, which
    # may resolve a shortfall that was waiting for capacity.
    from app.services.reconciliation import reconciler

    reconciler.wake()

    return await drain.build_drain_status(host)


@router.get("/{host_id}/drain", response_model=HostDrainStatus)
async def get_drain_status(host_id: str):
    """Report drain progress and blockers for a host (S-043 §5.3)."""
    host = _require_host(await host_db.get_host(host_id))
    return await drain.build_drain_status(host)


async def _proxy_instance_action(
    host_id: str,
    instance_id: str,
    action: str,
    method: str = "POST",
    timeout: float = 30,
    json_data: dict[str, Any] | None = None,
) -> Any:
    host = await host_db.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    try:
        async with aiohttp.ClientSession() as session:
            url = (
                f"{host.url}/instances/{instance_id}/{action}"
                if action
                else f"{host.url}/instances/{instance_id}"
            )
            headers = {"X-API-Key": host.api_key, "Content-Type": "application/json"}
            req_method = getattr(session, method.lower())
            kwargs: dict[str, Any] = {
                "headers": headers,
                "timeout": aiohttp.ClientTimeout(total=timeout),
            }
            if json_data is not None:
                kwargs["json"] = json_data
            async with req_method(url, **kwargs) as response:
                if response.status == 200:
                    return await response.json()
                text = await response.text()
                raise HTTPException(status_code=response.status, detail=text)
    except HTTPException:
        raise
    except (
        aiohttp.ClientConnectionError,
        aiohttp.ClientConnectorError,
        asyncio.TimeoutError,
    ):
        raise HTTPException(
            status_code=502, detail=f"Host '{host.name}' is unreachable at {host.url}"
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"Cannot reach host '{host.name}': {e}"
        )


async def _proxy_get(host: Host, path: str, *, timeout: int = 10) -> Any:
    """GET proxy to a host, returning parsed JSON."""
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"X-API-Key": host.api_key}
            async with session.get(
                f"{host.url}{path}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status == 200:
                    return await response.json()
                text = await response.text()
                raise HTTPException(status_code=response.status, detail=text)
    except HTTPException:
        raise
    except (
        aiohttp.ClientConnectionError,
        aiohttp.ClientConnectorError,
        asyncio.TimeoutError,
    ):
        raise HTTPException(
            status_code=502, detail=f"Host '{host.name}' is unreachable at {host.url}"
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"Cannot reach host '{host.name}': {e}"
        )


def _require_host(host: Host | None) -> Host:
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    return host


@router.post("/{host_id}/instances/{instance_id}/start")
async def start_instance(host_id: str, instance_id: str):
    # Blocking start: the host answers once the backend reports readiness,
    # which can take minutes for a cold model load.
    return await _proxy_instance_action(
        host_id, instance_id, "start", timeout=settings.host_start_timeout_s
    )


@router.post("/{host_id}/instances/{instance_id}/stop")
async def stop_instance(host_id: str, instance_id: str):
    return await _proxy_instance_action(host_id, instance_id, "stop")


@router.post("/{host_id}/instances/{instance_id}/restart")
async def restart_instance(host_id: str, instance_id: str):
    return await _proxy_instance_action(
        host_id, instance_id, "restart", timeout=settings.host_start_timeout_s
    )


@router.post("/{host_id}/instances")
async def create_instance(host_id: str, instance_data: dict[str, Any]):
    """Create an inference instance on a host.

    Delegates to the shared ``create_instance_on_host`` helper (S-037 /
    Option B refactor) which validates priority (S-036), resolves
    ``model_source`` (S-019), and POSTs to the host.
    """
    host = _require_host(await host_db.get_host(host_id))
    # Manual creation targets a host explicitly and so bypasses placement,
    # which is what keeps new work off a draining host (S-043 §3).
    if host.drain_state is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Host '{host.name}' is {host.drain_state.value} and does not "
                f"accept new instances. Resume the host first."
            ),
        )
    config = instance_data.get("config", instance_data)
    if isinstance(config, dict):
        validate_host_supports_backend(host, config.get("backend_type"))
    return await create_instance_on_host(host, instance_data)


@router.put("/{host_id}/instances/{instance_id}")
async def update_instance(
    host_id: str, instance_id: str, instance_data: dict[str, Any]
):
    # Validate priority if present (S-036)
    validate_priority(instance_data)
    return await _proxy_instance_action(
        host_id, instance_id, "", method="PUT", json_data=instance_data, timeout=10
    )


@router.delete("/{host_id}/instances/{instance_id}")
async def delete_instance(host_id: str, instance_id: str):
    return await _proxy_instance_action(
        host_id, instance_id, "", method="DELETE", timeout=10
    )


@router.get("/{host_id}/instances")
async def get_host_instances(host_id: str):
    host = _require_host(await host_db.get_host(host_id))
    return await _proxy_get(host, "/instances")


@router.get("/{host_id}/instances/{instance_id}/state")
async def get_instance_state(host_id: str, instance_id: str):
    host = _require_host(await host_db.get_host(host_id))
    return await _proxy_get(host, f"/instances/{instance_id}/state", timeout=5)


@router.get("/{host_id}/instances/{instance_id}/logs")
async def get_instance_logs(host_id: str, instance_id: str):
    host = _require_host(await host_db.get_host(host_id))
    return await _proxy_get(host, f"/instances/{instance_id}/logs")
