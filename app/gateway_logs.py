"""
Gateway event logging - async PostgreSQL storage.

Stores events and request summaries in PostgreSQL tables:
- gateway_events - All raw events
- gateway_requests - Request summaries (on completion)

Uses a write queue with periodic batch inserts for high throughput.
"""

import asyncio
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from collections import defaultdict

import asyncpg


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(ts_str: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp string to a datetime object."""
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def classify_request_type(endpoint: Optional[str]) -> str:
    """Classify request type based on endpoint."""
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
    """Tracks an in-flight request for building the summary."""

    request_id: str
    request_type: str = "unknown"
    model: Optional[str] = None
    resolved_model: Optional[str] = None
    endpoint: Optional[str] = None
    client_ip: Optional[str] = None
    stream: Optional[bool] = None
    start_timestamp: Optional[str] = None
    host_id: Optional[str] = None
    host_name: Optional[str] = None
    instance_id: Optional[str] = None
    instance_url: Optional[str] = None
    attempts: int = 0


@dataclass
class RequestSummary:
    """Final summary of a completed request."""

    request_id: str
    request_type: str
    status: str  # success | error | missed
    model: Optional[str]
    resolved_model: Optional[str]
    endpoint: Optional[str]
    client_ip: Optional[str]
    stream: Optional[bool]
    attempts: int
    start_timestamp: Optional[str]
    end_timestamp: str
    duration_s: Optional[float]
    host_id: Optional[str]
    host_name: Optional[str]
    instance_id: Optional[str]
    instance_url: Optional[str]
    error_message: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    decode_tps: Optional[float] = None
    decode_ms_per_token: Optional[float] = None


# SQL for table and index creation
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gateway_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    request_id TEXT,
    data JSONB NOT NULL DEFAULT '{}',
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON gateway_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON gateway_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_request_id ON gateway_events(request_id);

CREATE TABLE IF NOT EXISTS gateway_requests (
    id BIGSERIAL PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_type TEXT,
    status TEXT NOT NULL,
    model TEXT,
    resolved_model TEXT,
    endpoint TEXT,
    client_ip TEXT,
    stream BOOLEAN,
    attempts INTEGER DEFAULT 1,
    start_timestamp TIMESTAMPTZ,
    end_timestamp TIMESTAMPTZ NOT NULL,
    duration_s DOUBLE PRECISION,
    host_id TEXT,
    host_name TEXT,
    instance_id TEXT,
    instance_url TEXT,
    error_message TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    decode_tps DOUBLE PRECISION,
    decode_ms_per_token DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_requests_end_ts ON gateway_requests(end_timestamp);
CREATE INDEX IF NOT EXISTS idx_requests_status ON gateway_requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_model ON gateway_requests(model);
CREATE INDEX IF NOT EXISTS idx_requests_host ON gateway_requests(host_id);
CREATE INDEX IF NOT EXISTS idx_requests_type ON gateway_requests(request_type);
"""


class GatewayLogger:
    """Async gateway event logger with PostgreSQL storage."""

    FLUSH_INTERVAL_S = 1.0
    MAX_BUFFER_SIZE = 100

    def __init__(self) -> None:
        try:
            from app.config import settings
            self.database_url = settings.database_url
        except Exception:
            self.database_url = "postgresql://solar:solar@localhost:5432/solar_gateway"

        self._pool: Optional[asyncpg.Pool] = None
        self._inflight: Dict[str, RequestInProgress] = {}
        self._lock = asyncio.Lock()

        # Write buffers for batch inserts
        self._event_buffer: List[dict] = []
        self._request_buffer: List[dict] = []
        self._buffer_lock = asyncio.Lock()

        self._flush_task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

    async def start(self) -> None:
        """Create connection pool and ensure schema exists."""
        self._pool = await asyncpg.create_pool(
            self.database_url, min_size=2, max_size=10
        )
        # Auto-create tables and indexes
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA_SQL)
        # Start background flush loop
        self._stop_event = asyncio.Event()
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """Stop flush task and close pool."""
        if self._stop_event:
            self._stop_event.set()
        if self._flush_task:
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        # Final flush of remaining buffer
        await self._flush_all()
        if self._pool:
            await self._pool.close()
            self._pool = None

    async def _flush_loop(self) -> None:
        """Background task that periodically flushes the write buffer."""
        while self._stop_event and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.FLUSH_INTERVAL_S
                )
            except asyncio.TimeoutError:
                pass
            await self._flush_all()

    async def _flush_all(self) -> None:
        """Flush buffered events and requests to PostgreSQL."""
        async with self._buffer_lock:
            events = list(self._event_buffer)
            requests = list(self._request_buffer)
            self._event_buffer.clear()
            self._request_buffer.clear()

        if not self._pool:
            return

        if events:
            try:
                async with self._pool.acquire() as conn:
                    await conn.executemany(
                        """INSERT INTO gateway_events (event_type, request_id, data, timestamp)
                           VALUES ($1, $2, $3::jsonb, $4)""",
                        [
                            (
                                e["event_type"],
                                e.get("request_id"),
                                json.dumps(e["data"], default=str),
                                e["timestamp"],
                            )
                            for e in events
                        ],
                    )
            except Exception as exc:
                print(f"[GatewayLogger] Failed to flush events: {exc}")

        if requests:
            try:
                async with self._pool.acquire() as conn:
                    await conn.executemany(
                        """INSERT INTO gateway_requests (
                            request_id, request_type, status, model, resolved_model,
                            endpoint, client_ip, stream, attempts, start_timestamp,
                            end_timestamp, duration_s, host_id, host_name, instance_id,
                            instance_url, error_message, prompt_tokens, completion_tokens,
                            total_tokens, decode_tps, decode_ms_per_token
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22)
                        ON CONFLICT (request_id) DO NOTHING""",
                        [
                            (
                                r["request_id"],
                                r.get("request_type"),
                                r["status"],
                                r.get("model"),
                                r.get("resolved_model"),
                                r.get("endpoint"),
                                r.get("client_ip"),
                                r.get("stream"),
                                r.get("attempts", 1),
                                _parse_ts(r.get("start_timestamp")),
                                _parse_ts(r["end_timestamp"]),
                                r.get("duration_s"),
                                r.get("host_id"),
                                r.get("host_name"),
                                r.get("instance_id"),
                                r.get("instance_url"),
                                r.get("error_message"),
                                r.get("prompt_tokens"),
                                r.get("completion_tokens"),
                                r.get("total_tokens"),
                                r.get("decode_tps"),
                                r.get("decode_ms_per_token"),
                            )
                            for r in requests
                        ],
                    )
            except Exception as exc:
                print(f"[GatewayLogger] Failed to flush requests: {exc}")

    async def _queue_event(
        self, event_type: str, request_id: Optional[str], data: dict, timestamp: datetime
    ) -> None:
        """Queue a raw event for batch insert."""
        should_flush = False
        async with self._buffer_lock:
            self._event_buffer.append(
                {
                    "event_type": event_type,
                    "request_id": request_id,
                    "data": data,
                    "timestamp": timestamp,
                }
            )
            if len(self._event_buffer) >= self.MAX_BUFFER_SIZE:
                should_flush = True
        if should_flush:
            asyncio.create_task(self._flush_all())

    async def _queue_request(self, summary_dict: dict) -> None:
        """Queue a request summary for batch insert."""
        async with self._buffer_lock:
            self._request_buffer.append(summary_dict)

    async def log_event(self, event: Dict[str, Any]) -> Optional[RequestSummary]:
        """
        Log a gateway event asynchronously.

        Returns a RequestSummary if this event completes a request (for WebSocket broadcast).
        """
        etype = event.get("type")
        data = event.get("data") or {}
        timestamp = data.get("timestamp") or event.get("timestamp") or _utc_now_iso()
        request_id = data.get("request_id")

        # Add timestamp to event if missing
        if "timestamp" not in event:
            event["timestamp"] = timestamp

        # Parse timestamp for DB storage
        ts_dt = _parse_ts(timestamp) or datetime.now(timezone.utc)

        # Queue raw event write (non-blocking)
        await self._queue_event(etype or "unknown", request_id, data, ts_dt)

        if not request_id:
            return None

        # Track request state for summary building
        summary = None
        async with self._lock:
            if etype == "request_start":
                endpoint = data.get("endpoint")
                self._inflight[request_id] = RequestInProgress(
                    request_id=request_id,
                    request_type=classify_request_type(endpoint),
                    model=data.get("model"),
                    endpoint=endpoint,
                    client_ip=data.get("client_ip"),
                    stream=(
                        bool(data.get("stream"))
                        if data.get("stream") is not None
                        else None
                    ),
                    start_timestamp=timestamp,
                )

            elif etype == "request_routed":
                rip = self._inflight.get(request_id)
                if not rip:
                    # Missed the start, create minimal record
                    endpoint = data.get("endpoint")
                    rip = RequestInProgress(
                        request_id=request_id,
                        request_type=classify_request_type(endpoint),
                        model=data.get("model"),
                        endpoint=endpoint,
                        start_timestamp=timestamp,
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
                    # Missed start, create minimal
                    endpoint = data.get("endpoint")
                    rip = RequestInProgress(
                        request_id=request_id,
                        request_type=classify_request_type(endpoint),
                        model=data.get("model"),
                        endpoint=endpoint,
                        start_timestamp=timestamp,
                    )

                # Build summary
                status = (
                    "success"
                    if etype == "request_success"
                    else self._classify_error_status(data.get("error_message"))
                )
                duration = data.get("duration")

                # Token counts
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

                # Queue summary write (non-blocking)
                await self._queue_request(asdict(summary))

        return summary

    def _compute_duration(
        self, start_iso: Optional[str], end_iso: str
    ) -> Optional[float]:
        if not start_iso:
            return None
        try:
            s = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            e = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            return max(0.0, (e - s).total_seconds())
        except Exception:
            return None

    def _classify_error_status(self, message: Optional[str]) -> str:
        if not message:
            return "error"
        m = message.lower()
        if "no instances available" in m or ("model" in m and "not found" in m):
            return "missed"
        return "error"

    async def read_requests_async(
        self,
        start: datetime,
        end: datetime,
        status: Optional[str] = None,
        request_type: Optional[str] = None,
        model: Optional[str] = None,
        host_id: Optional[str] = None,
    ) -> List[dict]:
        """Read request summaries from PostgreSQL with filtering."""
        if not self._pool:
            return []

        query = "SELECT * FROM gateway_requests WHERE end_timestamp >= $1 AND end_timestamp <= $2"
        params: list = [start, end]
        idx = 3

        if status and status != "all":
            query += f" AND status = ${idx}"
            params.append(status)
            idx += 1
        if request_type and request_type != "all":
            query += f" AND request_type = ${idx}"
            params.append(request_type)
            idx += 1
        if model:
            query += f" AND (model = ${idx} OR resolved_model = ${idx})"
            params.append(model)
            idx += 1
        if host_id:
            query += f" AND host_id = ${idx}"
            params.append(host_id)
            idx += 1

        query += " ORDER BY end_timestamp DESC"

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        # Convert rows to dicts with ISO string timestamps
        results = []
        for row in rows:
            d = dict(row)
            # Remove DB-internal id
            d.pop("id", None)
            # Convert datetime fields to ISO strings for API compatibility
            for field in ("start_timestamp", "end_timestamp"):
                if isinstance(d.get(field), datetime):
                    d[field] = d[field].isoformat()
            results.append(d)
        return results

    async def read_events_async(
        self,
        start: datetime,
        end: datetime,
        types: Optional[List[str]] = None,
    ) -> List[dict]:
        """Read raw events from PostgreSQL with filtering."""
        if not self._pool:
            return []

        query = "SELECT * FROM gateway_events WHERE timestamp >= $1 AND timestamp <= $2"
        params: list = [start, end]
        idx = 3

        if types:
            placeholders = ", ".join(f"${idx + i}" for i in range(len(types)))
            query += f" AND event_type IN ({placeholders})"
            params.extend(types)
            idx += len(types)

        query += " ORDER BY timestamp ASC"

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        # Reconstruct event dict format matching the old JSONL structure
        results = []
        for row in rows:
            evt = {
                "type": row["event_type"],
                "data": json.loads(row["data"]) if isinstance(row["data"], str) else row["data"],
                "timestamp": row["timestamp"].isoformat(),
            }
            results.append(evt)
        return results


# Global singleton
gateway_logger = GatewayLogger()
