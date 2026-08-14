"""PostgreSQL-backed host CRUD operations using SQLAlchemy ORM."""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, update

from app.models import DrainState, Host, HostStatus, MemoryInfo

from .connection import get_session_factory
from .tables import HostRow


class HostDB:
    """Database-backed host management."""

    def _session(self):
        return get_session_factory()()

    def _row_to_host(self, row: HostRow) -> Host:
        memory = None
        if row.memory and isinstance(row.memory, dict):
            memory = MemoryInfo(**row.memory)

        roles: list[str] = []
        if isinstance(row.roles, list):
            roles = row.roles

        supported_backends: list[str] = []
        if isinstance(row.supported_backends, list):
            supported_backends = row.supported_backends

        return Host(
            id=row.id,
            name=row.name,
            url=row.url,
            api_key=row.api_key,
            status=HostStatus(row.status),
            last_seen=row.last_seen,
            memory=memory,
            gpu_type=row.gpu_type,
            roles=roles,
            supported_backends=supported_backends,
            disk_total_gb=row.disk_total_gb,
            disk_used_gb=row.disk_used_gb,
            disk_available_gb=row.disk_available_gb,
            memory_available_gb=row.memory_available_gb,
            version=row.version,
            drain_state=DrainState(row.drain_state) if row.drain_state else None,
            drain_requested_at=row.drain_requested_at,
            created_at=row.created_at,
        )

    def _host_to_dict(self, host: Host) -> dict[str, Any]:
        return {
            "id": host.id,
            "name": host.name,
            "url": host.url,
            "api_key": host.api_key,
            "status": host.status.value,
            "last_seen": host.last_seen,
            "memory": host.memory.model_dump() if host.memory else None,
            "gpu_type": host.gpu_type,
            "roles": host.roles or [],
            # None, not [], so a host registering without the field does not
            # look like a host that supports nothing.
            "supported_backends": host.supported_backends or None,
            "disk_total_gb": host.disk_total_gb,
            "disk_used_gb": host.disk_used_gb,
            "disk_available_gb": host.disk_available_gb,
            "memory_available_gb": host.memory_available_gb,
            "version": host.version,
            "drain_state": host.drain_state.value if host.drain_state else None,
            "drain_requested_at": host.drain_requested_at,
            "created_at": host.created_at,
        }

    async def add_host(self, host: Host) -> Host:
        async with self._session() as session:
            existing = await session.get(HostRow, host.id)
            if existing:
                values = self._host_to_dict(host)
                values.pop("id")
                values.pop("created_at")
                await session.execute(
                    update(HostRow).where(HostRow.id == host.id).values(**values)
                )
            else:
                session.add(HostRow(**self._host_to_dict(host)))
            await session.commit()
        return host

    async def remove_host(self, host_id: str) -> bool:
        async with self._session() as session:
            result = await session.execute(delete(HostRow).where(HostRow.id == host_id))
            await session.commit()
            return result.rowcount == 1

    async def get_host(self, host_id: str) -> Host | None:
        async with self._session() as session:
            row = await session.get(HostRow, host_id)
            return self._row_to_host(row) if row else None

    async def get_host_by_api_key(self, api_key: str) -> Host | None:
        async with self._session() as session:
            result = await session.execute(
                select(HostRow).where(HostRow.api_key == api_key)
            )
            row = result.scalar_one_or_none()
            return self._row_to_host(row) if row else None

    async def get_all_hosts(self, *, role: str | None = None) -> list[Host]:
        async with self._session() as session:
            stmt = select(HostRow).order_by(HostRow.created_at)
            if role:
                stmt = stmt.where(HostRow.roles.contains([role]))
            result = await session.execute(stmt)
            return [self._row_to_host(row) for row in result.scalars()]

    async def update_host_status(
        self,
        host_id: str,
        status: HostStatus,
        *,
        memory: dict[str, Any] | None = None,
    ) -> bool:
        values: dict[str, Any] = {"status": status.value}
        if status == HostStatus.ONLINE:
            values["last_seen"] = datetime.now(timezone.utc)
        if memory is not None:
            values["memory"] = memory

        async with self._session() as session:
            result = await session.execute(
                update(HostRow).where(HostRow.id == host_id).values(**values)
            )
            await session.commit()
            return result.rowcount == 1

    async def update_host_memory(
        self,
        host_id: str,
        memory: dict[str, Any],
        *,
        gpu_type: str | None = None,
        disk_total_gb: float | None = None,
        disk_used_gb: float | None = None,
        disk_available_gb: float | None = None,
        version: str | None = None,
    ) -> bool:
        values: dict[str, Any] = {
            "memory": memory,
            "last_seen": datetime.now(timezone.utc),
            "memory_available_gb": memory.get("available_gb"),
        }
        if gpu_type is not None:
            values["gpu_type"] = gpu_type
        if disk_total_gb is not None:
            values["disk_total_gb"] = disk_total_gb
        if disk_used_gb is not None:
            values["disk_used_gb"] = disk_used_gb
        if disk_available_gb is not None:
            values["disk_available_gb"] = disk_available_gb
        if version is not None:
            values["version"] = version

        async with self._session() as session:
            result = await session.execute(
                update(HostRow).where(HostRow.id == host_id).values(**values)
            )
            await session.commit()
            return result.rowcount == 1

    async def update_host_gpu_type(self, host_id: str, gpu_type: str) -> bool:
        async with self._session() as session:
            result = await session.execute(
                update(HostRow).where(HostRow.id == host_id).values(gpu_type=gpu_type)
            )
            await session.commit()
            return result.rowcount == 1

    async def update_host_roles(self, host_id: str, roles: list[str]) -> bool:
        async with self._session() as session:
            result = await session.execute(
                update(HostRow).where(HostRow.id == host_id).values(roles=roles)
            )
            await session.commit()
            return result.rowcount == 1

    async def set_drain_state(
        self, host_id: str, drain_state: DrainState | None
    ) -> Host | None:
        """Set (or clear) the drain state of *host_id* (S-043 §2.1).

        ``drain_requested_at`` is stamped when a drain starts and cleared
        when the host is resumed; the `draining` → `drained` promotion
        keeps the original request time.
        """
        values: dict[str, Any] = {
            "drain_state": drain_state.value if drain_state else None
        }
        if drain_state == DrainState.DRAINING:
            values["drain_requested_at"] = datetime.now(timezone.utc)
        elif drain_state is None:
            values["drain_requested_at"] = None

        async with self._session() as session:
            row = await session.get(HostRow, host_id)
            if row is None:
                return None
            for key, value in values.items():
                setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return self._row_to_host(row)

    async def list_draining_hosts(self) -> list[Host]:
        """List hosts with a drain state set, oldest request first."""
        async with self._session() as session:
            result = await session.execute(
                select(HostRow)
                .where(HostRow.drain_state.is_not(None))
                .order_by(HostRow.drain_requested_at)
            )
            return [self._row_to_host(row) for row in result.scalars()]

    async def update_host_supported_backends(
        self, host_id: str, supported_backends: list[str]
    ) -> bool:
        async with self._session() as session:
            result = await session.execute(
                update(HostRow)
                .where(HostRow.id == host_id)
                .values(supported_backends=supported_backends)
            )
            await session.commit()
            return result.rowcount == 1

    async def update_host_registration(
        self,
        host_id: str,
        *,
        gpu_type: str | None = None,
        roles: list[str] | None = None,
        supported_backends: list[str] | None = None,
        version: str | None = None,
    ) -> bool:
        """Persist the capability fields from a registration event."""
        values: dict[str, Any] = {}
        if gpu_type is not None:
            values["gpu_type"] = gpu_type
        if roles is not None:
            values["roles"] = roles
        if supported_backends is not None:
            values["supported_backends"] = supported_backends
        if version is not None:
            values["version"] = version
        if not values:
            return True

        async with self._session() as session:
            result = await session.execute(
                update(HostRow).where(HostRow.id == host_id).values(**values)
            )
            await session.commit()
            return result.rowcount == 1


host_db = HostDB()
