"""PostgreSQL-backed virtual model CRUD operations (S-060)."""

from datetime import datetime, timezone

from sqlalchemy import select

from app.models.virtual_model import (
    VirtualModelContract,
    VirtualModelCreate,
    VirtualModelResponse,
    VirtualModelUpdate,
)

from .connection import get_session_factory
from .tables import VirtualModelRow


class VirtualModelDB:
    """Database-backed virtual model management."""

    def _session(self):
        return get_session_factory()()

    def _row_to_response(self, row: VirtualModelRow) -> VirtualModelResponse:
        contract = VirtualModelContract(**(row.contract or {}))
        return VirtualModelResponse(
            id=str(row.id),
            name=row.name,
            targets=list(row.targets or []),
            description=row.description,
            contract=contract,
            created_at=row.created_at.isoformat() if row.created_at else None,
            updated_at=row.updated_at.isoformat() if row.updated_at else None,
        )

    async def list_all(self) -> list[VirtualModelResponse]:
        async with self._session() as session:
            result = await session.execute(
                select(VirtualModelRow).order_by(VirtualModelRow.name)
            )
            return [self._row_to_response(row) for row in result.scalars()]

    @staticmethod
    async def _get_row(session, name: str) -> VirtualModelRow | None:
        result = await session.execute(
            select(VirtualModelRow).where(VirtualModelRow.name == name)
        )
        return result.scalar_one_or_none()

    async def get_by_name(self, name: str) -> VirtualModelResponse | None:
        async with self._session() as session:
            row = await self._get_row(session, name)
            return self._row_to_response(row) if row else None

    async def create(self, data: VirtualModelCreate) -> VirtualModelResponse:
        """Insert a new virtual model. Raises ValueError when the name exists."""
        now = datetime.now(timezone.utc)
        async with self._session() as session:
            row = VirtualModelRow(
                name=data.name,
                targets=list(data.targets),
                description=data.description,
                contract=data.contract.model_dump(exclude_none=True),
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                await session.commit()
            except Exception as exc:  # noqa: BLE001 - unique violation -> 409
                await session.rollback()
                raise ValueError(f"virtual model '{data.name}' already exists") from exc
            await session.refresh(row)
            return self._row_to_response(row)

    async def update(
        self, name: str, data: VirtualModelUpdate
    ) -> VirtualModelResponse | None:
        """Replace mutable fields. Returns None when the name does not exist."""
        async with self._session() as session:
            row = await self._get_row(session, name)
            if not row:
                return None
            if data.targets is not None:
                row.targets = list(data.targets)
            if data.description is not None:
                row.description = data.description
            if data.contract is not None:
                row.contract = data.contract.model_dump(exclude_none=True)
            row.updated_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(row)
            return self._row_to_response(row)

    async def delete(self, name: str) -> bool:
        """Delete by name. Returns False when the name does not exist."""
        async with self._session() as session:
            row = await self._get_row(session, name)
            if not row:
                return False
            await session.delete(row)
            await session.commit()
            return True


virtual_model_db = VirtualModelDB()
