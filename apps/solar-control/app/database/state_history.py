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
    async def list_components(self, start: datetime, end: datetime) -> list[str]:
        """Distinct aliases sampled in the window, sorted.

        Only the alias is selected: the one caller groups its rows per alias, so
        the per-instance distinct work would be discarded.
        """
        table = InstanceStateHistoryRow
        stmt = (
            select(table.alias)
            .where(table.sampled_at >= start, table.sampled_at < end)
            .distinct()
            .order_by(table.alias)
        )
        async with self._session() as session:
            result = await session.execute(stmt)
            return list(result.scalars())

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

        One sampler tick writes one row per instance, so rows are first collapsed
        per ``(sampled_at, instance_id)``: an instance counts as *up* for a tick
        only when its own row is ``running``. The resulting instance-ticks are then
        counted per epoch-aligned bucket, so a replica set with one of two
        replicas down renders as a partial day instead of a full one. Buckets
        without samples are absent: the caller zero-fills them rather than
        interpolating.
        """
        table = InstanceStateHistoryRow
        ticks = select(
            table.sampled_at.label("sampled_at"),
            table.instance_id.label("instance_id"),
            func.bool_or(table.state == STATE_RUNNING).label("up"),
        ).where(table.sampled_at >= start, table.sampled_at < end)
        if aliases is not None:
            ticks = ticks.where(table.alias.in_(aliases))
        if instance_id is not None:
            ticks = ticks.where(table.instance_id == instance_id)
        ticks = ticks.group_by(table.sampled_at, table.instance_id).subquery()

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
