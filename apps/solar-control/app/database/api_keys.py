"""PostgreSQL-backed API key CRUD operations using SQLAlchemy ORM.

Keys belong to exactly one endpoint (``endpoint_id`` NOT NULL, CASCADE
delete). An endpoint can hold many keys; telemetry stays attributed to the
endpoint, and per-key visibility comes from ``last_used_at``.
"""

import secrets
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update

from .connection import get_session_factory
from .tables import ApiKeyRow


class ApiKey(BaseModel):
    """A credential for one API endpoint."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    endpoint_id: str
    name: str
    key: str = Field(default_factory=lambda: f"sk-{secrets.token_urlsafe(32)}")
    description: str | None = None
    enabled: bool = True
    last_used_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


_UNSET = object()


class ApiKeyDB:
    """Database-backed API key management."""

    def _session(self):
        return get_session_factory()()

    def _row_to_key(self, row: ApiKeyRow) -> ApiKey:
        return ApiKey(
            id=str(row.id),
            endpoint_id=str(row.endpoint_id),
            name=row.name,
            key=row.key,
            description=row.description,
            enabled=row.enabled,
            last_used_at=row.last_used_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def create(
        self,
        endpoint_id: str,
        name: str,
        *,
        description: str | None = None,
        key: str | None = None,
    ) -> ApiKey:
        ak = ApiKey(endpoint_id=endpoint_id, name=name, description=description)
        if key:
            ak.key = key
        async with self._session() as session:
            session.add(
                ApiKeyRow(
                    id=ak.id,
                    endpoint_id=ak.endpoint_id,
                    name=ak.name,
                    key=ak.key,
                    description=ak.description,
                    enabled=ak.enabled,
                    created_at=ak.created_at,
                    updated_at=ak.updated_at,
                )
            )
            await session.commit()
        return ak

    async def get(self, key_id: str) -> ApiKey | None:
        async with self._session() as session:
            row = await session.get(ApiKeyRow, key_id)
            return self._row_to_key(row) if row else None

    async def get_by_key(self, key: str) -> ApiKey | None:
        async with self._session() as session:
            result = await session.execute(
                select(ApiKeyRow).where(ApiKeyRow.key == key)
            )
            row = result.scalar_one_or_none()
            return self._row_to_key(row) if row else None

    async def list_for_endpoint(self, endpoint_id: str) -> list[ApiKey]:
        async with self._session() as session:
            result = await session.execute(
                select(ApiKeyRow)
                .where(ApiKeyRow.endpoint_id == endpoint_id)
                .order_by(ApiKeyRow.created_at)
            )
            return [self._row_to_key(row) for row in result.scalars()]

    async def list_all(self) -> list[ApiKey]:
        async with self._session() as session:
            result = await session.execute(
                select(ApiKeyRow).order_by(ApiKeyRow.created_at)
            )
            return [self._row_to_key(row) for row in result.scalars()]

    async def update(
        self,
        key_id: str,
        *,
        name: str | None = None,
        description: str | None = _UNSET,  # type: ignore[assignment]
        enabled: bool | None = None,
        endpoint_id: str | None = None,
    ) -> ApiKey | None:
        values: dict = {}
        if name is not None:
            values["name"] = name
        if description is not _UNSET:
            values["description"] = description
        if enabled is not None:
            values["enabled"] = enabled
        if endpoint_id is not None:
            values["endpoint_id"] = endpoint_id

        if not values:
            return await self.get(key_id)

        values["updated_at"] = datetime.now(timezone.utc)

        async with self._session() as session:
            result = await session.execute(
                update(ApiKeyRow)
                .where(ApiKeyRow.id == key_id)
                .values(**values)
                .returning(ApiKeyRow)
            )
            row = result.scalar_one_or_none()
            await session.commit()
            return self._row_to_key(row) if row else None

    async def rotate(self, key_id: str) -> ApiKey | None:
        """Replace the key material; every other field is left untouched."""
        new_key = f"sk-{secrets.token_urlsafe(32)}"
        async with self._session() as session:
            result = await session.execute(
                update(ApiKeyRow)
                .where(ApiKeyRow.id == key_id)
                .values(key=new_key, updated_at=datetime.now(timezone.utc))
                .returning(ApiKeyRow)
            )
            row = result.scalar_one_or_none()
            await session.commit()
            return self._row_to_key(row) if row else None

    async def delete(self, key_id: str) -> bool:
        async with self._session() as session:
            result = await session.execute(
                delete(ApiKeyRow).where(ApiKeyRow.id == key_id)
            )
            await session.commit()
            return result.rowcount == 1

    async def touch_last_used(self, key_id: str) -> None:
        """Stamp ``last_used_at`` without bumping ``updated_at``."""
        async with self._session() as session:
            await session.execute(
                update(ApiKeyRow)
                .where(ApiKeyRow.id == key_id)
                .values(last_used_at=func.now())
            )
            await session.commit()


api_key_db = ApiKeyDB()
