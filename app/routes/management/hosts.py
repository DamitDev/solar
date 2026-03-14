"""Host management API routes (under /api/hosts)."""

import uuid
import aiohttp
from typing import List
from fastapi import APIRouter, HTTPException

from app.models import Host, HostCreate, HostResponse, HostStatus
from app.database.hosts import host_db


router = APIRouter(prefix="/hosts", tags=["hosts"])


@router.post("", response_model=HostResponse)
async def register_host(data: HostCreate):
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{data.url}/health"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status != 200:
                    raise HTTPException(status_code=400, detail=f"Cannot connect to host at {data.url}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot connect to host: {str(e)}")

    host_id = str(uuid.uuid4())
    host = Host(id=host_id, name=data.name, url=data.url, api_key=data.api_key)

    try:
        async with aiohttp.ClientSession() as session:
            url = f"{data.url}/instances"
            headers = {"X-API-Key": data.api_key}
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    host.status = HostStatus.ONLINE
                    from datetime import datetime, timezone
                    host.last_seen = datetime.now(timezone.utc)
    except Exception:
        pass

    await host_db.add_host(host)
    return HostResponse(host=host, message=f"Host '{data.name}' registered successfully")


@router.get("", response_model=List[Host])
async def list_hosts():
    return await host_db.get_all_hosts()


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
    return HostResponse(host=host, message=f"Host '{host.name}' removed successfully")


@router.post("/{host_id}/refresh", response_model=HostResponse)
async def refresh_host_status(host_id: str):
    host = await host_db.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/health"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status != 200:
                    await host_db.update_host_status(host_id, HostStatus.ERROR)
                    raise HTTPException(status_code=400, detail=f"Health check failed: {response.status}")

            url = f"{host.url}/instances"
            headers = {"X-API-Key": host.api_key}
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    await host_db.update_host_status(host_id, HostStatus.ONLINE)
                    host = await host_db.get_host(host_id)
                    return HostResponse(host=host, message=f"Host '{host.name}' is online")
                else:
                    await host_db.update_host_status(host_id, HostStatus.ERROR)
                    raise HTTPException(status_code=400, detail=f"API auth failed: {response.status}")
    except HTTPException:
        raise
    except Exception as e:
        await host_db.update_host_status(host_id, HostStatus.OFFLINE)
        raise HTTPException(status_code=500, detail=f"Failed to connect: {str(e)}")


@router.post("/refresh-all")
async def refresh_all_hosts():
    hosts = await host_db.get_all_hosts()
    results = []
    async with aiohttp.ClientSession() as session:
        for host in hosts:
            try:
                url = f"{host.url}/health"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status != 200:
                        await host_db.update_host_status(host.id, HostStatus.ERROR)
                        results.append({"host_id": host.id, "name": host.name, "status": "error"})
                        continue
                url = f"{host.url}/instances"
                headers = {"X-API-Key": host.api_key}
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                    if response.status == 200:
                        await host_db.update_host_status(host.id, HostStatus.ONLINE)
                        results.append({"host_id": host.id, "name": host.name, "status": "online"})
                    else:
                        await host_db.update_host_status(host.id, HostStatus.ERROR)
                        results.append({"host_id": host.id, "name": host.name, "status": "error"})
            except Exception:
                await host_db.update_host_status(host.id, HostStatus.OFFLINE)
                results.append({"host_id": host.id, "name": host.name, "status": "offline"})
    return {"message": f"Refreshed {len(hosts)} hosts", "results": results}


# Proxy endpoints for instance management

@router.get("/{host_id}/instances")
async def get_host_instances(host_id: str):
    host = await host_db.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/instances"
            headers = {"X-API-Key": host.api_key}
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    return await response.json()
                raise HTTPException(status_code=response.status, detail="Failed to get instances")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


async def _proxy_instance_action(host_id: str, instance_id: str, action: str, method: str = "POST", timeout: int = 30, json_data: dict = None):
    host = await host_db.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/instances/{instance_id}/{action}" if action else f"{host.url}/instances/{instance_id}"
            headers = {"X-API-Key": host.api_key, "Content-Type": "application/json"}
            req_method = getattr(session, method.lower())
            kwargs = {"headers": headers, "timeout": aiohttp.ClientTimeout(total=timeout)}
            if json_data is not None:
                kwargs["json"] = json_data
            async with req_method(url, **kwargs) as response:
                if response.status == 200:
                    return await response.json()
                text = await response.text()
                raise HTTPException(status_code=response.status, detail=text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{host_id}/instances/{instance_id}/start")
async def start_instance(host_id: str, instance_id: str):
    return await _proxy_instance_action(host_id, instance_id, "start")


@router.post("/{host_id}/instances/{instance_id}/stop")
async def stop_instance(host_id: str, instance_id: str):
    return await _proxy_instance_action(host_id, instance_id, "stop")


@router.post("/{host_id}/instances/{instance_id}/restart")
async def restart_instance(host_id: str, instance_id: str):
    return await _proxy_instance_action(host_id, instance_id, "restart", timeout=60)


@router.post("/{host_id}/instances")
async def create_instance(host_id: str, instance_data: dict):
    host = await host_db.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/instances"
            headers = {"X-API-Key": host.api_key, "Content-Type": "application/json"}
            async with session.post(url, json=instance_data, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    return await response.json()
                text = await response.text()
                raise HTTPException(status_code=response.status, detail=text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{host_id}/instances/{instance_id}")
async def update_instance(host_id: str, instance_id: str, instance_data: dict):
    return await _proxy_instance_action(host_id, instance_id, "", method="PUT", json_data=instance_data, timeout=10)


@router.delete("/{host_id}/instances/{instance_id}")
async def delete_instance(host_id: str, instance_id: str):
    return await _proxy_instance_action(host_id, instance_id, "", method="DELETE", timeout=10)


@router.get("/{host_id}/instances/{instance_id}/state")
async def get_instance_state(host_id: str, instance_id: str):
    host = await host_db.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/instances/{instance_id}/state"
            headers = {"X-API-Key": host.api_key}
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status == 200:
                    return await response.json()
                text = await response.text()
                raise HTTPException(status_code=response.status, detail=text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{host_id}/instances/{instance_id}/logs")
async def get_instance_logs(host_id: str, instance_id: str):
    host = await host_db.get_host(host_id)
    if not host:
        raise HTTPException(status_code=404, detail="Host not found")
    try:
        async with aiohttp.ClientSession() as session:
            url = f"{host.url}/instances/{instance_id}/logs"
            headers = {"X-API-Key": host.api_key}
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status == 200:
                    return await response.json()
                text = await response.text()
                raise HTTPException(status_code=response.status, detail=text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
