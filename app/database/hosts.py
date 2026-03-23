"""PostgreSQL-backed host CRUD operations.

Replaces the file-based HostManager that used hosts.json.
"""

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from app.models import Host, HostStatus, MemoryInfo
from .connection import db_pool


class HostDB:
    """Database-backed host management."""

    async def add_host(self, host: Host) -> Host:
        pool = db_pool()
        memory_json = json.dumps(host.memory.model_dump()) if host.memory else None
        roles_json = json.dumps(host.roles) if host.roles else "[]"
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO hosts (id, name, url, api_key, status, last_seen, memory,
                       gpu_type, roles, disk_total_gb, disk_used_gb, disk_available_gb,
                       memory_available_gb, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9::jsonb,
                       $10, $11, $12, $13, $14)
                   ON CONFLICT (id) DO UPDATE SET
                       name = EXCLUDED.name,
                       url = EXCLUDED.url,
                       api_key = EXCLUDED.api_key,
                       status = EXCLUDED.status,
                       last_seen = EXCLUDED.last_seen,
                       memory = EXCLUDED.memory,
                       gpu_type = EXCLUDED.gpu_type,
                       roles = EXCLUDED.roles,
                       disk_total_gb = EXCLUDED.disk_total_gb,
                       disk_used_gb = EXCLUDED.disk_used_gb,
                       disk_available_gb = EXCLUDED.disk_available_gb,
                       memory_available_gb = EXCLUDED.memory_available_gb""",
                host.id,
                host.name,
                host.url,
                host.api_key,
                host.status.value,
                host.last_seen,
                memory_json,
                host.gpu_type,
                roles_json,
                host.disk_total_gb,
                host.disk_used_gb,
                host.disk_available_gb,
                host.memory_available_gb,
                host.created_at,
            )
        return host

    async def remove_host(self, host_id: str) -> bool:
        pool = db_pool()
        async with pool.acquire() as conn:
            result = await conn.execute("DELETE FROM hosts WHERE id = $1", host_id)
        return result == "DELETE 1"

    async def get_host(self, host_id: str) -> Optional[Host]:
        pool = db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM hosts WHERE id = $1", host_id)
        if not row:
            return None
        return self._row_to_host(row)

    async def get_host_by_api_key(self, api_key: str) -> Optional[Host]:
        pool = db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM hosts WHERE api_key = $1", api_key)
        if not row:
            return None
        return self._row_to_host(row)

    async def get_all_hosts(self) -> List[Host]:
        pool = db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM hosts ORDER BY created_at")
        return [self._row_to_host(row) for row in rows]

    async def update_host_status(
        self,
        host_id: str,
        status: HostStatus,
        *,
        memory: Optional[Dict[str, Any]] = None,
    ) -> bool:
        pool = db_pool()
        now = datetime.now(timezone.utc) if status == HostStatus.ONLINE else None
        memory_json = json.dumps(memory) if memory else None

        async with pool.acquire() as conn:
            if memory_json is not None and now is not None:
                result = await conn.execute(
                    "UPDATE hosts SET status = $2, last_seen = $3, memory = $4::jsonb WHERE id = $1",
                    host_id,
                    status.value,
                    now,
                    memory_json,
                )
            elif now is not None:
                result = await conn.execute(
                    "UPDATE hosts SET status = $2, last_seen = $3 WHERE id = $1",
                    host_id,
                    status.value,
                    now,
                )
            else:
                result = await conn.execute(
                    "UPDATE hosts SET status = $2 WHERE id = $1",
                    host_id,
                    status.value,
                )
        return result == "UPDATE 1"

    async def update_host_memory(
        self,
        host_id: str,
        memory: Dict[str, Any],
        *,
        gpu_type: Optional[str] = None,
        disk_total_gb: Optional[float] = None,
        disk_used_gb: Optional[float] = None,
        disk_available_gb: Optional[float] = None,
    ) -> bool:
        pool = db_pool()
        now = datetime.now(timezone.utc)
        memory_available_gb = memory.get("available_gb") if memory else None

        sets = ["memory = $2::jsonb", "last_seen = $3", "memory_available_gb = $4"]
        params: list = [host_id, json.dumps(memory), now, memory_available_gb]
        idx = 5

        if gpu_type is not None:
            sets.append(f"gpu_type = ${idx}")
            params.append(gpu_type)
            idx += 1
        if disk_total_gb is not None:
            sets.append(f"disk_total_gb = ${idx}")
            params.append(disk_total_gb)
            idx += 1
        if disk_used_gb is not None:
            sets.append(f"disk_used_gb = ${idx}")
            params.append(disk_used_gb)
            idx += 1
        if disk_available_gb is not None:
            sets.append(f"disk_available_gb = ${idx}")
            params.append(disk_available_gb)
            idx += 1

        sql = f"UPDATE hosts SET {', '.join(sets)} WHERE id = $1"
        async with pool.acquire() as conn:
            result = await conn.execute(sql, *params)
        return result == "UPDATE 1"

    async def update_host_gpu_type(self, host_id: str, gpu_type: str) -> bool:
        pool = db_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE hosts SET gpu_type = $2 WHERE id = $1",
                host_id,
                gpu_type,
            )
        return result == "UPDATE 1"

    async def update_host_roles(self, host_id: str, roles: list) -> bool:
        pool = db_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE hosts SET roles = $2::jsonb WHERE id = $1",
                host_id,
                json.dumps(roles),
            )
        return result == "UPDATE 1"

    def _row_to_host(self, row) -> Host:
        memory = None
        if row["memory"]:
            raw = (
                row["memory"]
                if isinstance(row["memory"], dict)
                else json.loads(row["memory"])
            )
            memory = MemoryInfo(**raw)

        raw_roles = row.get("roles")
        if raw_roles is None:
            roles = []
        elif isinstance(raw_roles, list):
            roles = raw_roles
        else:
            roles = json.loads(raw_roles)

        return Host(
            id=row["id"],
            name=row["name"],
            url=row["url"],
            api_key=row["api_key"],
            status=HostStatus(row["status"]),
            last_seen=row["last_seen"],
            memory=memory,
            gpu_type=row.get("gpu_type"),
            roles=roles,
            disk_total_gb=row.get("disk_total_gb"),
            disk_used_gb=row.get("disk_used_gb"),
            disk_available_gb=row.get("disk_available_gb"),
            memory_available_gb=row.get("memory_available_gb"),
            created_at=row["created_at"],
        )


host_db = HostDB()
