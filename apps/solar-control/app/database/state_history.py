"""PostgreSQL-backed instance state history (batch insert + windowed reads)."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert, select

from .connection import get_session_factory, isolated_db_op
from .tables import InstanceStateHistoryRow


@dataclass(frozen=True)
class StateSample:
    """One instance state observation at a point in time."""

    alias: str
    instance_id: str
    host_id: str | None
    state: str
    sampled_at: datetime


class StateHistoryDB:
    """Database-backed instance state history."""

    def _session(self):
        return get_session_factory()()

    @staticmethod
    def _row_to_sample(row: InstanceStateHistoryRow) -> StateSample:
        return StateSample(
            alias=row.alias,
            instance_id=row.instance_id,
            host_id=row.host_id,
            state=row.state,
            sampled_at=row.sampled_at,
        )

    @isolated_db_op
    async def insert_batch(self, samples: list[StateSample]) -> int:
        """Insert all samples in one statement. Returns the number written."""
        if not samples:
            return 0
        values = [
            {
                "alias": s.alias,
                "instance_id": s.instance_id,
                "host_id": s.host_id,
                "state": s.state,
                "sampled_at": s.sampled_at,
            }
            for s in samples
        ]
        async with self._session() as session:
            await session.execute(insert(InstanceStateHistoryRow), values)
            await session.commit()
        return len(values)

    @isolated_db_op
    async def read_window(
        self,
        start: datetime,
        end: datetime,
        *,
        alias: str | None = None,
        instance_id: str | None = None,
    ) -> list[StateSample]:
        """Samples with ``start <= sampled_at < end``, oldest first.

        ``alias`` / ``instance_id`` narrow the read; both hit a composite
        index leading with the filtered column.
        """
        stmt = select(InstanceStateHistoryRow).where(
            InstanceStateHistoryRow.sampled_at >= start,
            InstanceStateHistoryRow.sampled_at < end,
        )
        if alias is not None:
            stmt = stmt.where(InstanceStateHistoryRow.alias == alias)
        if instance_id is not None:
            stmt = stmt.where(InstanceStateHistoryRow.instance_id == instance_id)
        stmt = stmt.order_by(
            InstanceStateHistoryRow.sampled_at, InstanceStateHistoryRow.id
        )
        async with self._session() as session:
            result = await session.execute(stmt)
            return [self._row_to_sample(row) for row in result.scalars()]


state_history_db = StateHistoryDB()
