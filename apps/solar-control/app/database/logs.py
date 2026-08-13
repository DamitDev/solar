"""Gateway event logging - async PostgreSQL storage via SQLAlchemy.

Stores events and request summaries in PostgreSQL tables:
- gateway_events  -- all raw events
- gateway_requests -- request summaries (on completion)

Uses a write queue with periodic batch inserts for high throughput.
"""

import asyncio
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Text, and_, select
from sqlalchemy import func as sa_func
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .connection import get_session_factory
from .tables import GatewayEventRow, GatewayRequestRow

logger = logging.getLogger(__name__)

INFLIGHT_MAX_AGE_S = 900

# Bucket ladder for gateway time series, in seconds. `auto` walks it in order
# and takes the first bucket that keeps a range under TIMESERIES_MAX_POINTS.
TIMESERIES_BUCKETS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "6h": 21600,
    "1d": 86400,
    "7d": 604800,
}
TIMESERIES_MAX_POINTS = 180


def resolve_bucket(start: datetime, end: datetime, requested: str | None) -> str:
    """Pick a bucket size for a range, honouring an explicit request."""
    if requested and requested != "auto" and requested in TIMESERIES_BUCKETS:
        return requested

    span_s = max(0.0, (end - start).total_seconds())
    for name, seconds in TIMESERIES_BUCKETS.items():
        if span_s / seconds <= TIMESERIES_MAX_POINTS:
            return name
    return "7d"


def fill_buckets(
    start: datetime,
    end: datetime,
    bucket_s: int,
    aggregates: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expand sparse per-bucket aggregates into a gap-free series.

    A missing bucket means no traffic, so it has to be emitted as zeros --
    otherwise a chart silently interpolates across an outage.
    """
    points: list[dict[str, Any]] = []
    epoch = int(start.timestamp()) // bucket_s * bucket_s
    last = int(end.timestamp())

    while epoch <= last:
        agg = aggregates.get(epoch)
        points.append(
            {
                "ts": datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(),
                "success": agg["success"] if agg else 0,
                "error": agg["error"] if agg else 0,
                "missed": agg["missed"] if agg else 0,
                "token_in": agg["token_in"] if agg else 0,
                "token_out": agg["token_out"] if agg else 0,
                "avg_duration_s": agg["avg_duration_s"] if agg else None,
            }
        )
        epoch += bucket_s

    return points


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(ts_str: str | None) -> datetime | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def classify_request_type(endpoint: str | None) -> str:
    if not endpoint:
        return "unknown"
    ep = endpoint.lower()
    if "/embeddings" in ep:
        return "embedding"
    if "/chat/completions" in ep:
        return "chat"
    if "/completions" in ep:
        return "completion"
    if "/classify" in ep:
        return "classification"
    if "/rerank" in ep:
        return "rerank"
    if "/tokenize" in ep:
        return "tokenize"
    if "/detokenize" in ep:
        return "detokenize"
    return "unknown"


@dataclass
class RequestInProgress:
    request_id: str
    request_type: str = "unknown"
    model: str | None = None
    resolved_model: str | None = None
    endpoint: str | None = None
    endpoint_id: str | None = None
    client_ip: str | None = None
    stream: bool | None = None
    start_timestamp: str | None = None
    host_id: str | None = None
    host_name: str | None = None
    instance_id: str | None = None
    instance_url: str | None = None
    attempts: int = 0
    created_at: float = 0.0


@dataclass
class RequestSummary:
    request_id: str
    request_type: str
    status: str
    model: str | None
    resolved_model: str | None
    endpoint: str | None
    endpoint_id: str | None
    client_ip: str | None
    stream: bool | None
    attempts: int
    start_timestamp: str | None
    end_timestamp: str
    duration_s: float | None
    host_id: str | None
    host_name: str | None
    instance_id: str | None
    instance_url: str | None
    error_message: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    decode_tps: float | None = None
    decode_ms_per_token: float | None = None


class GatewayLogger:
    """Async gateway event logger with PostgreSQL storage."""

    FLUSH_INTERVAL_S = 1.0
    MAX_BUFFER_SIZE = 100

    def __init__(self) -> None:
        self._inflight: dict[str, RequestInProgress] = {}
        self._lock = asyncio.Lock()
        self._event_buffer: list[dict[str, Any]] = []
        self._request_buffer: list[dict[str, Any]] = []
        self._buffer_lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    async def start(self) -> None:
        self._stop_event = asyncio.Event()
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._flush_task:
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self._flush_all()

    async def _flush_loop(self) -> None:
        while self._stop_event and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.FLUSH_INTERVAL_S
                )
            except asyncio.TimeoutError:
                pass
            await self._flush_all()
            self._reap_stale_inflight()

    def _reap_stale_inflight(self) -> None:
        """Remove _inflight entries older than INFLIGHT_MAX_AGE_S (leaked by client disconnects)."""
        now = time.monotonic()
        stale = [
            rid
            for rid, rip in self._inflight.items()
            if rip.created_at > 0 and (now - rip.created_at) > INFLIGHT_MAX_AGE_S
        ]
        for rid in stale:
            self._inflight.pop(rid, None)
        if stale:
            logger.warning("Reaped %d stale inflight request(s)", len(stale))

    async def _flush_all(self) -> None:
        async with self._buffer_lock:
            events = list(self._event_buffer)
            requests = list(self._request_buffer)
            self._event_buffer.clear()
            self._request_buffer.clear()

        try:
            session_factory = get_session_factory()
        except RuntimeError:
            return

        if events:
            try:
                async with session_factory() as session:
                    for e in events:
                        session.add(
                            GatewayEventRow(
                                event_type=e["event_type"],
                                request_id=e.get("request_id"),
                                endpoint_id=e.get("endpoint_id"),
                                data=e["data"],
                                timestamp=e["timestamp"],
                            )
                        )
                    await session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to flush events: %s", exc)

        if requests:
            try:
                async with session_factory() as session:
                    for r in requests:
                        stmt = (
                            pg_insert(GatewayRequestRow)
                            .values(
                                request_id=r["request_id"],
                                request_type=r.get("request_type"),
                                status=r["status"],
                                model=r.get("model"),
                                resolved_model=r.get("resolved_model"),
                                endpoint=r.get("endpoint"),
                                endpoint_id=r.get("endpoint_id"),
                                client_ip=r.get("client_ip"),
                                stream=r.get("stream"),
                                attempts=r.get("attempts", 1),
                                start_timestamp=_parse_ts(r.get("start_timestamp")),
                                end_timestamp=_parse_ts(r["end_timestamp"]),
                                duration_s=r.get("duration_s"),
                                host_id=r.get("host_id"),
                                host_name=r.get("host_name"),
                                instance_id=r.get("instance_id"),
                                instance_url=r.get("instance_url"),
                                error_message=r.get("error_message"),
                                prompt_tokens=r.get("prompt_tokens"),
                                completion_tokens=r.get("completion_tokens"),
                                total_tokens=r.get("total_tokens"),
                                decode_tps=r.get("decode_tps"),
                                decode_ms_per_token=r.get("decode_ms_per_token"),
                            )
                            .on_conflict_do_nothing(index_elements=["request_id"])
                        )
                        await session.execute(stmt)
                    await session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to flush requests: %s", exc)

    async def _queue_event(
        self,
        event_type: str,
        request_id: str | None,
        endpoint_id: str | None,
        data: dict[str, Any],
        timestamp: datetime,
    ) -> None:
        should_flush = False
        async with self._buffer_lock:
            self._event_buffer.append(
                {
                    "event_type": event_type,
                    "request_id": request_id,
                    "endpoint_id": endpoint_id,
                    "data": data,
                    "timestamp": timestamp,
                }
            )
            if len(self._event_buffer) >= self.MAX_BUFFER_SIZE:
                should_flush = True
        if should_flush:
            asyncio.create_task(self._flush_all())

    async def _queue_request(self, summary_dict: dict[str, Any]) -> None:
        async with self._buffer_lock:
            self._request_buffer.append(summary_dict)

    async def log_event(
        self, event: dict[str, Any], *, endpoint_id: str | None = None
    ) -> RequestSummary | None:
        """Log a gateway event.

        Returns a RequestSummary when the event completes a request lifecycle.
        """
        etype = event.get("type")
        data = event.get("data") or {}
        timestamp = data.get("timestamp") or event.get("timestamp") or _utc_now_iso()
        request_id = data.get("request_id")

        if "timestamp" not in event:
            event["timestamp"] = timestamp

        ts_dt = _parse_ts(timestamp) or datetime.now(timezone.utc)
        await self._queue_event(
            etype or "unknown", request_id, endpoint_id, data, ts_dt
        )

        if not request_id:
            return None

        summary = None
        async with self._lock:
            if etype == "request_start":
                ep = data.get("endpoint")
                self._inflight[request_id] = RequestInProgress(
                    request_id=request_id,
                    request_type=classify_request_type(ep),
                    model=data.get("model"),
                    endpoint=ep,
                    endpoint_id=endpoint_id,
                    client_ip=data.get("client_ip"),
                    stream=(
                        bool(data.get("stream"))
                        if data.get("stream") is not None
                        else None
                    ),
                    start_timestamp=timestamp,
                    created_at=time.monotonic(),
                )

            elif etype == "request_routed":
                rip = self._inflight.get(request_id)
                if not rip:
                    ep = data.get("endpoint")
                    rip = RequestInProgress(
                        request_id=request_id,
                        request_type=classify_request_type(ep),
                        model=data.get("model"),
                        endpoint=ep,
                        endpoint_id=endpoint_id,
                        start_timestamp=timestamp,
                        created_at=time.monotonic(),
                    )
                    self._inflight[request_id] = rip

                rip.attempts += 1
                rip.resolved_model = data.get("resolved_model") or rip.resolved_model
                rip.host_id = data.get("host_id") or rip.host_id
                rip.host_name = data.get("host_name") or rip.host_name
                rip.instance_id = data.get("instance_id") or rip.instance_id
                rip.instance_url = data.get("instance_url") or rip.instance_url
                rip.client_ip = data.get("client_ip") or rip.client_ip

            elif etype in ("request_success", "request_error"):
                rip = self._inflight.pop(request_id, None)
                if not rip:
                    ep = data.get("endpoint")
                    rip = RequestInProgress(
                        request_id=request_id,
                        request_type=classify_request_type(ep),
                        model=data.get("model"),
                        endpoint=ep,
                        endpoint_id=endpoint_id,
                        start_timestamp=timestamp,
                    )

                status = (
                    "success"
                    if etype == "request_success"
                    else self._classify_error_status(data.get("error_message"))
                )
                duration = data.get("duration")

                p_tok = (
                    data.get("prompt_tokens")
                    if isinstance(data.get("prompt_tokens"), (int, float))
                    else None
                )
                c_tok = (
                    data.get("completion_tokens")
                    if isinstance(data.get("completion_tokens"), (int, float))
                    else None
                )
                t_tok = (
                    data.get("total_tokens")
                    if isinstance(data.get("total_tokens"), (int, float))
                    else None
                )
                if t_tok is None and p_tok is not None and c_tok is not None:
                    t_tok = int(p_tok) + int(c_tok)

                decode_tps = (
                    float(data["decode_tps"])
                    if isinstance(data.get("decode_tps"), (int, float))
                    else None
                )
                decode_ms = (
                    float(data["decode_ms_per_token"])
                    if isinstance(data.get("decode_ms_per_token"), (int, float))
                    else None
                )

                summary = RequestSummary(
                    request_id=request_id,
                    request_type=rip.request_type,
                    status=status,
                    model=rip.model,
                    resolved_model=rip.resolved_model,
                    endpoint=rip.endpoint,
                    endpoint_id=rip.endpoint_id or endpoint_id,
                    client_ip=rip.client_ip,
                    stream=rip.stream,
                    attempts=max(1, rip.attempts),
                    start_timestamp=rip.start_timestamp,
                    end_timestamp=timestamp,
                    duration_s=(
                        float(duration)
                        if duration is not None
                        else self._compute_duration(rip.start_timestamp, timestamp)
                    ),
                    host_id=rip.host_id or data.get("host_id"),
                    host_name=rip.host_name or data.get("host_name"),
                    instance_id=rip.instance_id or data.get("instance_id"),
                    instance_url=rip.instance_url,
                    error_message=data.get("error_message"),
                    prompt_tokens=int(p_tok) if p_tok is not None else None,
                    completion_tokens=int(c_tok) if c_tok is not None else None,
                    total_tokens=int(t_tok) if t_tok is not None else None,
                    decode_tps=decode_tps,
                    decode_ms_per_token=decode_ms,
                )
                await self._queue_request(asdict(summary))

        return summary

    def _compute_duration(self, start_iso: str | None, end_iso: str) -> float | None:
        if not start_iso:
            return None
        try:
            s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            return max(0.0, (e - s).total_seconds())
        except Exception:  # noqa: BLE001
            return None

    def _classify_error_status(self, message: str | None) -> str:
        if not message:
            return "error"
        m = message.lower()
        if "no instances available" in m or ("model" in m and "not found" in m):
            return "missed"
        return "error"

    # ── SQL-level reads with server-side pagination ───────────

    def _build_request_conditions(
        self,
        start: datetime,
        end: datetime,
        *,
        status: str | None = None,
        request_type: str | None = None,
        model: str | None = None,
        host_id: str | None = None,
        endpoint_id: str | None = None,
    ) -> list:
        R = GatewayRequestRow
        conditions = [R.end_timestamp >= start, R.end_timestamp <= end]
        if status and status != "all":
            conditions.append(R.status == status)
        if request_type and request_type != "all":
            conditions.append(R.request_type == request_type)
        if model:
            conditions.append((R.model == model) | (R.resolved_model == model))
        if host_id:
            conditions.append(R.host_id == host_id)
        if endpoint_id:
            conditions.append(R.endpoint_id == endpoint_id)
        return conditions

    @staticmethod
    def _row_to_dict(row: GatewayRequestRow) -> dict[str, Any]:
        return {
            "request_id": row.request_id,
            "request_type": row.request_type,
            "status": row.status,
            "model": row.model,
            "resolved_model": row.resolved_model,
            "endpoint": row.endpoint,
            "endpoint_id": str(row.endpoint_id) if row.endpoint_id else None,
            "client_ip": row.client_ip,
            "stream": row.stream,
            "attempts": row.attempts,
            "start_timestamp": (
                row.start_timestamp.isoformat() if row.start_timestamp else None
            ),
            "end_timestamp": (
                row.end_timestamp.isoformat() if row.end_timestamp else None
            ),
            "duration_s": row.duration_s,
            "host_id": row.host_id,
            "host_name": row.host_name,
            "instance_id": row.instance_id,
            "instance_url": row.instance_url,
            "error_message": row.error_message,
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "total_tokens": row.total_tokens,
            "decode_tps": row.decode_tps,
            "decode_ms_per_token": row.decode_ms_per_token,
        }

    async def read_requests_page(
        self,
        start: datetime,
        end: datetime,
        *,
        status: str | None = None,
        request_type: str | None = None,
        model: str | None = None,
        host_id: str | None = None,
        endpoint_id: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Read paginated requests. Returns (items, total_count)."""
        await self._flush_all()

        try:
            session_factory = get_session_factory()
        except RuntimeError:
            return [], 0

        conditions = self._build_request_conditions(
            start,
            end,
            status=status,
            request_type=request_type,
            model=model,
            host_id=host_id,
            endpoint_id=endpoint_id,
        )

        async with session_factory() as session:
            count_stmt = select(sa_func.count()).select_from(
                select(GatewayRequestRow.id).where(and_(*conditions)).subquery()
            )
            total = (await session.execute(count_stmt)).scalar() or 0

            stmt = (
                select(GatewayRequestRow)
                .where(and_(*conditions))
                .order_by(GatewayRequestRow.end_timestamp.desc())
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()

        return [self._row_to_dict(row) for row in rows], total

    async def read_stats(
        self,
        start: datetime,
        end: datetime,
        *,
        request_type: str | None = None,
        endpoint_id: str | None = None,
    ) -> dict[str, Any]:
        """Compute gateway stats entirely in SQL -- no full result set in memory."""
        await self._flush_all()

        try:
            session_factory = get_session_factory()
        except RuntimeError:
            return {"completed": 0, "missed": 0, "error": 0}

        R = GatewayRequestRow
        conditions = self._build_request_conditions(
            start,
            end,
            request_type=request_type,
            endpoint_id=endpoint_id,
        )

        async with session_factory() as session:
            agg_stmt = select(
                sa_func.count().filter(R.status == "success").label("completed"),
                sa_func.count().filter(R.status == "missed").label("missed"),
                sa_func.count().filter(R.status == "error").label("error"),
                sa_func.sum(R.prompt_tokens)
                .filter(R.status == "success")
                .label("token_in_total"),
                sa_func.sum(R.completion_tokens)
                .filter(R.status == "success")
                .label("token_out_total"),
                sa_func.count()
                .filter(R.status == "success", R.prompt_tokens.isnot(None))
                .label("p_count"),
                sa_func.count()
                .filter(R.status == "success", R.completion_tokens.isnot(None))
                .label("c_count"),
            ).where(and_(*conditions))
            row = (await session.execute(agg_stmt)).one()

            completed = row.completed or 0
            missed = row.missed or 0
            error = row.error or 0
            token_in_total = int(row.token_in_total or 0)
            token_out_total = int(row.token_out_total or 0)
            p_count = row.p_count or 0
            c_count = row.c_count or 0

            model_key = sa_func.coalesce(R.resolved_model, R.model, "unknown")
            model_stmt = (
                select(
                    model_key.label("model_key"),
                    sa_func.count().label("completed"),
                    sa_func.coalesce(sa_func.sum(R.prompt_tokens), 0).label("token_in"),
                    sa_func.coalesce(sa_func.sum(R.completion_tokens), 0).label(
                        "token_out"
                    ),
                    sa_func.coalesce(sa_func.avg(R.duration_s), 0).label(
                        "avg_duration_s"
                    ),
                )
                .where(and_(*conditions, R.status == "success"))
                .group_by(model_key)
            )
            model_rows = (await session.execute(model_stmt)).all()

            host_stmt = (
                select(
                    R.host_id,
                    sa_func.max(R.host_name).label("host_name"),
                    sa_func.count().label("completed"),
                    sa_func.coalesce(sa_func.sum(R.prompt_tokens), 0).label("token_in"),
                    sa_func.coalesce(sa_func.sum(R.completion_tokens), 0).label(
                        "token_out"
                    ),
                    sa_func.coalesce(sa_func.avg(R.duration_s), 0).label(
                        "avg_duration_s"
                    ),
                )
                .where(and_(*conditions, R.host_id.isnot(None)))
                .group_by(R.host_id)
            )
            host_rows = (await session.execute(host_stmt)).all()

            E = GatewayEventRow
            ev_conditions = [
                E.timestamp >= start,
                E.timestamp <= end,
                E.event_type == "request_reroute",
            ]
            if endpoint_id:
                ev_conditions.append(E.endpoint_id == endpoint_id)
            reroute_stmt = select(sa_func.count(sa_func.distinct(E.request_id))).where(
                and_(*ev_conditions)
            )
            rerouted_unique = (await session.execute(reroute_stmt)).scalar() or 0

        return {
            "completed": completed,
            "missed": missed,
            "error": error,
            "rerouted_requests": rerouted_unique,
            "token_in_total": token_in_total,
            "token_out_total": token_out_total,
            "avg_tokens_in": (token_in_total / p_count) if p_count else 0,
            "avg_tokens_out": (token_out_total / c_count) if c_count else 0,
            "models": [
                {
                    "model": r.model_key,
                    "completed": r.completed,
                    "token_in": int(r.token_in),
                    "token_out": int(r.token_out),
                    "avg_duration_s": float(r.avg_duration_s),
                }
                for r in model_rows
            ],
            "hosts": [
                {
                    "host_id": r.host_id,
                    "host_name": r.host_name or r.host_id,
                    "completed": r.completed,
                    "token_in": int(r.token_in),
                    "token_out": int(r.token_out),
                    "avg_duration_s": float(r.avg_duration_s),
                }
                for r in host_rows
            ],
        }

    def _group_key_expr(self, group_by: str):
        """Column to break the time series down by, or None for a flat series."""
        R = GatewayRequestRow
        if group_by == "endpoint":
            return sa_func.cast(R.endpoint_id, Text)
        if group_by == "model":
            return sa_func.coalesce(R.resolved_model, R.model)
        if group_by == "host":
            return R.host_id
        return None

    async def read_timeseries(
        self,
        start: datetime,
        end: datetime,
        *,
        bucket: str | None = None,
        group_by: str | None = None,
        request_type: str | None = None,
        model: str | None = None,
        host_id: str | None = None,
        endpoint_id: str | None = None,
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        """Bucketed request/token/latency counts.

        Returns ``(bucket, points, series)``. ``points`` is always the combined
        series; ``series`` holds the per-group breakdown when ``group_by`` is
        set, so a caller gets both from one query. Empty buckets are filled with
        zeros so a gap in the chart reads as "no traffic" rather than as an
        interpolated line.
        """
        await self._flush_all()

        bucket_name = resolve_bucket(start, end, bucket)
        bucket_s = TIMESERIES_BUCKETS[bucket_name]

        try:
            session_factory = get_session_factory()
        except RuntimeError:
            return bucket_name, [], []

        R = GatewayRequestRow
        conditions = self._build_request_conditions(
            start,
            end,
            request_type=request_type,
            model=model,
            host_id=host_id,
            endpoint_id=endpoint_id,
        )

        # date_trunc only handles calendar units, so floor the epoch instead --
        # that covers every entry in the ladder with one expression.
        bucket_expr = sa_func.to_timestamp(
            sa_func.floor(sa_func.extract("epoch", R.end_timestamp) / bucket_s)
            * bucket_s
        )

        group_expr = self._group_key_expr(group_by or "none")
        columns = [
            bucket_expr.label("ts"),
            sa_func.count().filter(R.status == "success").label("success"),
            sa_func.count().filter(R.status == "error").label("error"),
            sa_func.count().filter(R.status == "missed").label("missed"),
            sa_func.coalesce(
                sa_func.sum(R.prompt_tokens).filter(R.status == "success"), 0
            ).label("token_in"),
            sa_func.coalesce(
                sa_func.sum(R.completion_tokens).filter(R.status == "success"), 0
            ).label("token_out"),
            sa_func.sum(R.duration_s).filter(R.status == "success").label("duration_s"),
        ]
        group_cols: list = [bucket_expr]
        if group_expr is not None:
            columns.append(group_expr.label("group_key"))
            group_cols.append(group_expr)

        stmt = (
            select(*columns)
            .where(and_(*conditions))
            .group_by(*group_cols)
            .order_by(bucket_expr)
        )

        async with session_factory() as session:
            rows = (await session.execute(stmt)).all()

        return (
            bucket_name,
            *self._shape_timeseries(
                rows, start, end, bucket_s, grouped=group_expr is not None
            ),
        )

    @staticmethod
    def _shape_timeseries(
        rows,
        start: datetime,
        end: datetime,
        bucket_s: int,
        *,
        grouped: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Turn raw SQL rows into a combined series plus per-group series."""

        def blank() -> dict[str, Any]:
            return {
                "success": 0,
                "error": 0,
                "missed": 0,
                "token_in": 0,
                "token_out": 0,
                "duration_s": 0.0,
            }

        def accumulate(target: dict[str, Any], row) -> None:
            target["success"] += row.success
            target["error"] += row.error
            target["missed"] += row.missed
            target["token_in"] += int(row.token_in)
            target["token_out"] += int(row.token_out)
            target["duration_s"] += float(row.duration_s or 0.0)

        def finalize(agg: dict[str, Any]) -> dict[str, Any]:
            # Sum-then-divide keeps the combined average weighted by volume; a
            # mean of per-group means would over-weight quiet groups.
            return {
                **{k: v for k, v in agg.items() if k != "duration_s"},
                "avg_duration_s": (
                    agg["duration_s"] / agg["success"] if agg["success"] else None
                ),
            }

        totals: dict[int, dict[str, Any]] = {}
        by_group: dict[str, dict[int, dict[str, Any]]] = {}

        for row in rows:
            epoch = int(row.ts.timestamp())
            accumulate(totals.setdefault(epoch, blank()), row)
            if grouped:
                key = row.group_key or "unknown"
                accumulate(by_group.setdefault(key, {}).setdefault(epoch, blank()), row)

        points = fill_buckets(
            start, end, bucket_s, {k: finalize(v) for k, v in totals.items()}
        )

        series = [
            {
                "key": key,
                "total": sum(
                    b["success"] + b["error"] + b["missed"] for b in buckets.values()
                ),
                "points": fill_buckets(
                    start, end, bucket_s, {k: finalize(v) for k, v in buckets.items()}
                ),
            }
            for key, buckets in by_group.items()
        ]
        series.sort(key=lambda s: s["total"], reverse=True)

        return points, series

    async def read_events(
        self,
        start: datetime,
        end: datetime,
        *,
        types: list[str] | None = None,
        endpoint_id: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        await self._flush_all()

        try:
            session_factory = get_session_factory()
        except RuntimeError:
            return []

        E = GatewayEventRow
        conditions = [E.timestamp >= start, E.timestamp <= end]

        if types:
            conditions.append(E.event_type.in_(types))
        if endpoint_id:
            conditions.append(E.endpoint_id == endpoint_id)

        stmt = (
            select(E).where(and_(*conditions)).order_by(E.timestamp.desc()).limit(limit)
        )

        async with session_factory() as session:
            result = await session.execute(stmt)
            rows = result.scalars().all()

        results: list[dict[str, Any]] = []
        for row in reversed(rows):
            evt: dict[str, Any] = {
                "type": row.event_type,
                "data": row.data if isinstance(row.data, dict) else {},
                "timestamp": row.timestamp.isoformat(),
            }
            if row.endpoint_id:
                evt["endpoint_id"] = str(row.endpoint_id)
            results.append(evt)
        return results


gateway_logger = GatewayLogger()
