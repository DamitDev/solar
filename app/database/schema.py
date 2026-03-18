"""PostgreSQL schema definitions and migration logic."""

from .connection import db_pool

MIGRATIONS_SQL = """
ALTER TABLE hosts ADD COLUMN IF NOT EXISTS gpu_type TEXT;
"""

SCHEMA_SQL = """
-- API endpoints (multi-tenant OpenAI gateways)
CREATE TABLE IF NOT EXISTS api_endpoints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    api_key TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Solar hosts (migrated from hosts.json)
CREATE TABLE IF NOT EXISTS hosts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    api_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'offline',
    last_seen TIMESTAMPTZ,
    memory JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Gateway raw events
CREATE TABLE IF NOT EXISTS gateway_events (
    id BIGSERIAL PRIMARY KEY,
    event_type TEXT NOT NULL,
    request_id TEXT,
    endpoint_id UUID REFERENCES api_endpoints(id) ON DELETE SET NULL,
    data JSONB NOT NULL DEFAULT '{}',
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON gateway_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON gateway_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_request_id ON gateway_events(request_id);
CREATE INDEX IF NOT EXISTS idx_events_endpoint ON gateway_events(endpoint_id);

-- Gateway request summaries
CREATE TABLE IF NOT EXISTS gateway_requests (
    id BIGSERIAL PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    request_type TEXT,
    status TEXT NOT NULL,
    model TEXT,
    resolved_model TEXT,
    endpoint TEXT,
    endpoint_id UUID REFERENCES api_endpoints(id) ON DELETE SET NULL,
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
CREATE INDEX IF NOT EXISTS idx_requests_endpoint ON gateway_requests(endpoint_id);
"""


async def ensure_schema() -> None:
    """Create all tables and indexes if they don't exist, then run migrations."""
    pool = db_pool()
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_SQL)
        await conn.execute(MIGRATIONS_SQL)
