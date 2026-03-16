"""PostgreSQL-backed API endpoint CRUD operations.

Each API endpoint represents a tenant (dev, uat, prod) with its own API key.
All endpoints serve the same models but have separate request logging.
"""

import uuid
import secrets
from datetime import datetime, timezone
from typing import List, Optional

from pydantic import BaseModel, Field
from .connection import db_pool


class ApiEndpoint(BaseModel):
    """An OpenAI-compatible API endpoint (tenant)."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    api_key: str = Field(default_factory=lambda: f"sk-{secrets.token_urlsafe(32)}")
    description: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EndpointDB:
    """Database-backed API endpoint management."""

    async def create_endpoint(
        self,
        name: str,
        *,
        description: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> ApiEndpoint:
        ep = ApiEndpoint(name=name, description=description)
        if api_key:
            ep.api_key = api_key
        pool = db_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO api_endpoints (id, name, api_key, description, created_at, updated_at)
                   VALUES ($1::uuid, $2, $3, $4, $5, $6)""",
                uuid.UUID(ep.id),
                ep.name,
                ep.api_key,
                ep.description,
                ep.created_at,
                ep.updated_at,
            )
        return ep

    async def get_endpoint(self, endpoint_id: str) -> Optional[ApiEndpoint]:
        pool = db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM api_endpoints WHERE id = $1::uuid",
                uuid.UUID(endpoint_id),
            )
        if not row:
            return None
        return self._row_to_endpoint(row)

    async def get_endpoint_by_api_key(self, api_key: str) -> Optional[ApiEndpoint]:
        pool = db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM api_endpoints WHERE api_key = $1", api_key
            )
        if not row:
            return None
        return self._row_to_endpoint(row)

    async def get_all_endpoints(self) -> List[ApiEndpoint]:
        pool = db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM api_endpoints ORDER BY created_at")
        return [self._row_to_endpoint(row) for row in rows]

    async def update_endpoint(
        self,
        endpoint_id: str,
        *,
        name: Optional[str] = None,
        description: Optional[str] = ...,  # type: ignore[assignment]
        api_key: Optional[str] = None,
    ) -> Optional[ApiEndpoint]:
        pool = db_pool()
        sets: list[str] = []
        params: list = []
        idx = 2  # $1 is the id

        if name is not None:
            sets.append(f"name = ${idx}")
            params.append(name)
            idx += 1
        if description is not ...:
            sets.append(f"description = ${idx}")
            params.append(description)
            idx += 1
        if api_key is not None:
            sets.append(f"api_key = ${idx}")
            params.append(api_key)
            idx += 1

        if not sets:
            return await self.get_endpoint(endpoint_id)

        sets.append(f"updated_at = ${idx}")
        params.append(datetime.now(timezone.utc))

        query = f"UPDATE api_endpoints SET {', '.join(sets)} WHERE id = $1::uuid RETURNING *"
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, uuid.UUID(endpoint_id), *params)
        if not row:
            return None
        return self._row_to_endpoint(row)

    async def delete_endpoint(self, endpoint_id: str) -> bool:
        pool = db_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM api_endpoints WHERE id = $1::uuid", uuid.UUID(endpoint_id)
            )
        return result == "DELETE 1"

    async def get_usage_stats(self, endpoint_id: str, *, hours: int = 24) -> dict:
        """Get usage statistics for a specific endpoint."""
        pool = db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT
                       COUNT(*) as total_requests,
                       COUNT(*) FILTER (WHERE status = 'success') as successful_requests,
                       COUNT(*) FILTER (WHERE status = 'error') as error_requests,
                       COUNT(*) FILTER (WHERE status = 'missed') as missed_requests,
                       COALESCE(SUM(prompt_tokens), 0) as total_prompt_tokens,
                       COALESCE(SUM(completion_tokens), 0) as total_completion_tokens,
                       COALESCE(SUM(total_tokens), 0) as total_tokens,
                       AVG(duration_s) FILTER (WHERE status = 'success') as avg_duration_s,
                       AVG(decode_tps) FILTER (WHERE decode_tps IS NOT NULL) as avg_decode_tps
                   FROM gateway_requests
                   WHERE endpoint_id = $1::uuid
                     AND end_timestamp >= NOW() - make_interval(hours => $2)""",
                uuid.UUID(endpoint_id),
                hours,
            )
        if not row:
            return {}
        return {
            "total_requests": row["total_requests"],
            "successful_requests": row["successful_requests"],
            "error_requests": row["error_requests"],
            "missed_requests": row["missed_requests"],
            "total_prompt_tokens": row["total_prompt_tokens"],
            "total_completion_tokens": row["total_completion_tokens"],
            "total_tokens": row["total_tokens"],
            "avg_duration_s": (
                float(row["avg_duration_s"]) if row["avg_duration_s"] else None
            ),
            "avg_decode_tps": (
                float(row["avg_decode_tps"]) if row["avg_decode_tps"] else None
            ),
        }

    def _row_to_endpoint(self, row) -> ApiEndpoint:
        return ApiEndpoint(
            id=str(row["id"]),
            name=row["name"],
            api_key=row["api_key"],
            description=row["description"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


endpoint_db = EndpointDB()
