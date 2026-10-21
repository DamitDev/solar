"""PostgreSQL-backed instance state history (batch insert + windowed reads)."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, insert, literal_column, select

from .connection import get_session_factory, isolated_db_op
from .tables import InstanceStateHistoryRow

STATE_RUNNING = "running"
STATE_OFFLINE = "offline"


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

    @isolated_db_op
    async def delete_older_than(self, cutoff: datetime) -> int:
        """Delete samples with ``sampled_at < cutoff``. Returns rows deleted.

        A sample stamped exactly at *cutoff* is kept.
        """
        stmt = delete(InstanceStateHistoryRow).where(
            InstanceStateHistoryRow.sampled_at < cutoff
        )
        async with self._session() as session:
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)

    @isolated_db_op
    async def list_components(
        self, start: datetime, end: datetime
    ) -> list[tuple[str, str]]:
        """Distinct ``(alias, instance_id)`` pairs sampled in the window."""
        table = InstanceStateHistoryRow
        stmt = (
            select(table.alias, table.instance_id)
            .where(table.sampled_at >= start, table.sampled_at < end)
            .distinct()
            .order_by(table.alias, table.instance_id)
        )
        async with self._session() as session:
            result = await session.execute(stmt)
            return [(row.alias, row.instance_id) for row in result]

    @isolated_db_op
    async def availability_buckets(
        self,
        start: datetime,
        end: datetime,
        bucket_s: int,
        *,
        aliases: list[str] | None = None,
        instance_id: str | None = None,
    ) -> dict[int, tuple[int, int]]:
        """Per-bucket ``(ticks, ticks_up)`` over ``start <= sampled_at < end``.

        One sampler tick shares a single ``sampled_at``, so rows are first
        collapsed per tick: a tick counts as *up* when any matched row is
        ``running`` (several aliases or replicas form one component). Ticks
        are then counted per epoch-aligned bucket. Buckets without samples
        are absent: the caller zero-fills them rather than interpolating.
        """
        table = InstanceStateHistoryRow
        ticks = select(
            table.sampled_at.label("sampled_at"),
            func.bool_or(table.state == STATE_RUNNING).label("up"),
        ).where(table.sampled_at >= start, table.sampled_at < end)
        if aliases is not None:
            ticks = ticks.where(table.alias.in_(aliases))
        if instance_id is not None:
            ticks = ticks.where(table.instance_id == instance_id)
        ticks = ticks.group_by(table.sampled_at).subquery()

        # Inlined (an int, never user text) so the SELECT and GROUP BY
        # expressions are textually identical; bound parameters would render
        # as distinct placeholders and Postgres would reject the grouping.
        width = literal_column(str(int(bucket_s)))
        bucket = func.floor(func.extract("epoch", ticks.c.sampled_at) / width) * width
        stmt = select(
            bucket.label("bucket"),
            func.count().label("total"),
            func.count().filter(ticks.c.up).label("up"),
        ).group_by(bucket)
        async with self._session() as session:
            result = await session.execute(stmt)
            return {int(row.bucket): (int(row.total), int(row.up)) for row in result}


state_history_db = StateHistoryDB()
