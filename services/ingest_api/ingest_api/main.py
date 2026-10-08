"""ingest-api: authenticated, rate-limited HTTP front door for events.

Validates batches, stamps each event with an id, and publishes to RabbitMQ.
It never touches the database: ingestion stays fast and decoupled from storage.
"""
import hashlib
import hmac
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import redis.asyncio as aioredis
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from prometheus_client import Counter

from sentinel_common import mq
from sentinel_common.config import get_settings
from sentinel_common.logging import setup_logging
from sentinel_common.models import EventBatch
from sentinel_common.web import install_ops

SERVICE = "ingest-api"
log = logging.getLogger(SERVICE)
settings = get_settings()

EVENTS_INGESTED = Counter("sentinel_events_ingested_total", "Events accepted", ["service"])
BATCHES_REJECTED = Counter("sentinel_batches_rejected_total", "Batches rejected", ["reason"])


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(SERVICE, settings.log_level)
    app.state.settings = settings
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    app.state.connection = await mq.connect(settings.amqp_url)
    channel = await app.state.connection.channel()
    app.state.exchange, _ = await mq.declare_topology(channel)
    log.info("ingest-api started")
    yield
    await app.state.connection.close()
    await app.state.redis.aclose()


app = FastAPI(title="SentinelOps Ingest API", version="1.0.0", lifespan=lifespan)


async def _redis_ready() -> bool:
    return bool(await app.state.redis.ping())


async def _mq_ready() -> bool:
    return not app.state.connection.is_closed


install_ops(app, SERVICE, {"redis": _redis_ready, "rabbitmq": _mq_ready})


async def authenticate(request: Request, x_api_key: str | None = Header(default=None)) -> str:
    keys = request.app.state.settings.api_key_set
    if not x_api_key or not any(hmac.compare_digest(x_api_key, k) for k in keys):
        BATCHES_REJECTED.labels("unauthorized").inc()
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
    return x_api_key


async def within_rate_limit(redis, api_key: str, n: int, limit: int, now: float | None = None) -> bool:
    """Fixed-window limiter, counted in *events* per minute per API key."""
    now = now or time.time()
    key = f"rl:{hashlib.sha256(api_key.encode()).hexdigest()[:12]}:{int(now // 60)}"
    count = await redis.incrby(key, n)
    if count == n:
        await redis.expire(key, 90)
    return count <= limit


@app.post("/v1/events", status_code=202)
async def ingest(batch: EventBatch, request: Request, api_key: str = Depends(authenticate)):
    cfg = request.app.state.settings
    if len(batch.events) > cfg.max_batch_size:
        BATCHES_REJECTED.labels("too_large").inc()
        raise HTTPException(413, f"max batch size is {cfg.max_batch_size}")
    if not await within_rate_limit(request.app.state.redis, api_key, len(batch.events), cfg.rate_limit_per_minute):
        BATCHES_REJECTED.labels("rate_limited").inc()
        retry = 60 - int(time.time() % 60)
        raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(retry)})

    received = datetime.now(UTC)
    events = []
    for ev in batch.events:
        doc = ev.model_dump(mode="json")
        ts = ev.ts or received
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        doc.update(event_id=str(uuid.uuid4()), ts=ts.isoformat(), is_error=ev.is_error)
        events.append(doc)

    batch_id = str(uuid.uuid4())
    try:
        await mq.publish_json(
            request.app.state.exchange,
            mq.RK_RAW,
            {"batch_id": batch_id, "received_at": received.isoformat(), "events": events},
        )
    except Exception as exc:  # noqa: BLE001
        log.error("publish failed", extra={"error": str(exc)})
        BATCHES_REJECTED.labels("publish_failed").inc()
        raise HTTPException(503, "event bus unavailable, retry later") from exc

    for ev in batch.events:
        EVENTS_INGESTED.labels(ev.service).inc()
    return {"accepted": len(events), "batch_id": batch_id}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_config=None)
