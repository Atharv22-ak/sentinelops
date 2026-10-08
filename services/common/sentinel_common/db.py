import asyncio
import logging

import asyncpg

log = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id          BIGSERIAL PRIMARY KEY,
    event_id    UUID UNIQUE NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    service     TEXT NOT NULL,
    level       TEXT NOT NULL,
    status_code INT,
    latency_ms  DOUBLE PRECISION,
    message     TEXT,
    trace_id    TEXT,
    attrs       JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_error    BOOLEAN NOT NULL DEFAULT FALSE,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_service_ts ON events (service, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_errors ON events (ts DESC) WHERE is_error;

CREATE TABLE IF NOT EXISTS incidents (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    fingerprint  TEXT NOT NULL,
    service      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    severity     TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open',
    summary      TEXT NOT NULL,
    details      JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurrences  INT NOT NULL DEFAULT 1,
    opened_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    acked_at     TIMESTAMPTZ,
    acked_by     TEXT,
    resolved_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents (status, opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_fp ON incidents (fingerprint) WHERE status <> 'resolved';
"""

_LOCK_ID = 727274


async def create_pool(dsn: str, attempts: int = 60, delay: float = 2.0) -> asyncpg.Pool:
    for attempt in range(1, attempts + 1):
        try:
            return await asyncpg.create_pool(dsn, min_size=1, max_size=10, command_timeout=30)
        except Exception as exc:  # noqa: BLE001
            log.warning("postgres not ready", extra={"attempt": attempt, "error": str(exc)})
            await asyncio.sleep(delay)
    raise RuntimeError("could not connect to Postgres")


async def ensure_schema(pool: asyncpg.Pool) -> None:
    """Idempotent migration; advisory lock avoids races between replicas."""
    async with pool.acquire() as conn:
        await conn.execute(f"SELECT pg_advisory_lock({_LOCK_ID})")
        try:
            await conn.execute(SCHEMA_SQL)
        finally:
            await conn.execute(f"SELECT pg_advisory_unlock({_LOCK_ID})")
