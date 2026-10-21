"""PostgreSQL-backed status message CRUD operations."""

from datetime import datetime, timezone

from sqlalchemy import select

from app.models.status_message import (
    StatusMessageCreate,
    StatusMessageResponse,
    StatusMessageUpdate,
)

from .connection import get_session_factory, isolated_db_op
from .tables import StatusMessageRow


class StatusMessageDB:
    """Database-backed status message management."""

    def _session(self):
        return get_session_factory()()

    @staticmethod
    def _row_to_response(row: StatusMessageRow) -> StatusMessageResponse:
        return StatusMessageResponse(
            id=str(row.id),
            body=row.body,
            type=row.type,
            start_time=row.start_time,
            end_time=row.end_time,
            created_at=row.created_at,
        )

    @staticmethod
    async def _get_row(session, message_id: str) -> StatusMessageRow | None:
        result = await session.execute(
            select(StatusMessageRow).where(StatusMessageRow.id == message_id)
        )
        return result.scalar_one_or_none()

    @isolated_db_op
    async def list_all(self) -> list[StatusMessageResponse]:
        """All messages, newest start first."""
        async with self._session() as session:
            result = await session.execute(
                select(StatusMessageRow).order_by(
                    StatusMessageRow.start_time.desc(), StatusMessageRow.created_at
                )
            )
            return [self._row_to_response(row) for row in result.scalars()]

    @isolated_db_op
    async def get(self, message_id: str) -> StatusMessageResponse | None:
        async with self._session() as session:
            row = await self._get_row(session, message_id)
            return self._row_to_response(row) if row else None

    @isolated_db_op
    async def create(self, data: StatusMessageCreate) -> StatusMessageResponse:
        async with self._session() as session:
            row = StatusMessageRow(
                body=data.body,
                type=data.type,
                start_time=data.start_time,
                end_time=data.end_time,
                created_at=datetime.now(timezone.utc),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._row_to_response(row)

    @isolated_db_op
    async def update(
        self, message_id: str, data: StatusMessageUpdate
    ) -> StatusMessageResponse | None:
        """Apply the fields set on ``data``. Returns None when the id is unknown."""
        async with self._session() as session:
            row = await self._get_row(session, message_id)
            if not row:
                return None
            for field in data.model_fields_set:
                value = getattr(data, field)
                # Only end_time is nullable; null elsewhere means "unchanged".
                if value is not None or field == "end_time":
                    setattr(row, field, value)
            await session.commit()
            await session.refresh(row)
            return self._row_to_response(row)

    @isolated_db_op
    async def delete(self, message_id: str) -> bool:
        """Delete by id. Returns False when the id does not exist."""
        async with self._session() as session:
            row = await self._get_row(session, message_id)
            if not row:
                return False
            await session.delete(row)
            await session.commit()
            return True


status_message_db = StatusMessageDB()
