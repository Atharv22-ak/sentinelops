"""query-api: read API + live dashboard for events, services and incidents."""
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sentinel_common import db
from sentinel_common.config import get_settings
from sentinel_common.logging import setup_logging
from sentinel_common.web import install_ops

SERVICE = "query-api"
log = logging.getLogger(SERVICE)
settings = get_settings()
STATIC = Path(__file__).parent / "static"
ERR = "(is_error)"


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(SERVICE, settings.log_level)
    app.state.pool = await db.create_pool(settings.postgres_dsn)
    await db.ensure_schema(app.state.pool)
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    log.info("query-api started")
    yield
    await app.state.pool.close()
    await app.state.redis.aclose()


app = FastAPI(title="SentinelOps Query API", version="1.0.0", lifespan=lifespan)


async def _pg_ready() -> bool:
    async with app.state.pool.acquire() as conn:
        return await conn.fetchval("SELECT 1") == 1


install_ops(app, SERVICE, {"postgres": _pg_ready})


async def cached(key: str, ttl: int, producer):
    """Tiny read-through cache (Redis) so dashboard polling doesn't hammer Postgres."""
    hit = await app.state.redis.get(key)
    if hit:
        return json.loads(hit)
    value = await producer()
    await app.state.redis.set(key, json.dumps(value, default=str), ex=ttl)
    return value


@app.get("/v1/services")
async def services():
    async def produce():
        sql = """
        SELECT service, count(*) AS events,
               count(*) FILTER (WHERE is_error) AS errors,
               round(avg(latency_ms)::numeric, 1) AS avg_latency_ms,
               round((percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms))::numeric, 1) AS p95_latency_ms,
               max(ts) AS last_seen
        FROM events WHERE ts > now() - interval '5 minutes' GROUP BY service ORDER BY service"""
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(sql)
        out = []
        for r in rows:
            d = dict(r)
            d["error_rate"] = round(d["errors"] / d["events"], 4) if d["events"] else 0
            out.append(d)
        return out

    return await cached("cache:services", 5, produce)


@app.get("/v1/events")
async def events(service: str | None = None, level: str | None = None, errors_only: bool = False,
                 minutes: int = Query(15, ge=1, le=2880), limit: int = Query(100, ge=1, le=1000)):
    sql = """SELECT event_id, ts, service, level, status_code, latency_ms, message, trace_id, attrs
             FROM events WHERE ts > now() - make_interval(mins => $1)
               AND ($2::text IS NULL OR service = $2)
               AND ($3::text IS NULL OR level = $3)
               AND (NOT $4 OR is_error)
             ORDER BY ts DESC LIMIT $5"""
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(sql, minutes, service, level.upper() if level else None, errors_only, limit)
    return [dict(r) for r in rows]


@app.get("/v1/stats/timeseries")
async def timeseries(service: str | None = None, minutes: int = Query(60, ge=5, le=1440)):
    async def produce():
        sql = """SELECT date_trunc('minute', ts) AS minute, count(*) AS events,
                        count(*) FILTER (WHERE is_error) AS errors,
                        round(avg(latency_ms)::numeric, 1) AS avg_latency_ms
                 FROM events WHERE ts > now() - make_interval(mins => $1) AND ($2::text IS NULL OR service = $2)
                 GROUP BY 1 ORDER BY 1"""
        async with app.state.pool.acquire() as conn:
            return [dict(r) for r in await conn.fetch(sql, minutes, service)]

    return await cached(f"cache:ts:{service}:{minutes}", 5, produce)


@app.get("/v1/incidents")
async def incidents(status: str | None = Query(None, pattern="^(open|acknowledged|resolved)$"),
                    limit: int = Query(50, ge=1, le=500)):
    sql = """SELECT * FROM incidents WHERE ($1::text IS NULL OR status = $1)
             ORDER BY opened_at DESC LIMIT $2"""
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(sql, status, limit)
    return [dict(r) for r in rows]


class AckBody(BaseModel):
    by: str = "dashboard"


@app.post("/v1/incidents/{incident_id}/ack")
async def ack(incident_id: UUID, body: AckBody | None = None):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE incidents SET status='acknowledged', acked_at=now(), acked_by=$2
               WHERE id=$1 AND status='open' RETURNING *""", incident_id, (body or AckBody()).by)
    if not row:
        raise HTTPException(404, "incident not found or not open")
    return dict(row)


@app.post("/v1/incidents/{incident_id}/resolve")
async def resolve(incident_id: UUID):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE incidents SET status='resolved', resolved_at=now() WHERE id=$1 AND status<>'resolved' RETURNING *",
            incident_id)
    if not row:
        raise HTTPException(404, "incident not found or already resolved")
    return dict(row)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/", include_in_schema=False)
async def dashboard():
    return FileResponse(STATIC / "index.html")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_config=None)
