"""processor: consumes event batches, persists them, feeds the sliding windows and
runs the anomaly detector (leader-elected through a Redis lock so N replicas stay safe)."""
import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI
from prometheus_client import Counter, Gauge, Histogram

from sentinel_common import db, mq
from sentinel_common.config import get_settings
from sentinel_common.logging import setup_logging
from sentinel_common.web import install_ops

from . import windows
from .detector import DetectorConfig, check_silence, evaluate

SERVICE = "processor"
log = logging.getLogger(SERVICE)
settings = get_settings()
INSTANCE = f"{SERVICE}-{uuid.uuid4().hex[:8]}"

EVENTS_PROCESSED = Counter("sentinel_events_processed_total", "Events persisted")
BATCH_SECONDS = Histogram("sentinel_batch_processing_seconds", "Batch processing time")
INCIDENTS_PUBLISHED = Counter("sentinel_incidents_published_total", "Incidents published", ["kind"])
IS_LEADER = Gauge("sentinel_detector_is_leader", "1 if this replica runs the detector")
DETECTOR_TICKS = Counter("sentinel_detector_ticks_total", "Detector evaluations")

INSERT_SQL = """
INSERT INTO events (event_id, ts, service, level, status_code, latency_ms, message, trace_id, attrs, is_error)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
ON CONFLICT (event_id) DO NOTHING
"""

# Atomic "acquire or renew" leader lock.
LEADER_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
elseif redis.call('set', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then
  return 1
else
  return 0
end
"""


def detector_config() -> DetectorConfig:
    s = settings
    return DetectorConfig(s.min_events, s.error_rate_floor, s.latency_floor_ms, s.z_threshold,
                          s.ewma_alpha, s.warmup_samples, s.heartbeat_timeout_sec)


async def handle_batch(app: FastAPI, body: dict) -> None:
    events = body["events"]
    received = datetime.fromisoformat(body["received_at"])
    rows = [
        (e["event_id"], datetime.fromisoformat(e["ts"]), e["service"], e["level"], e.get("status_code"),
         e.get("latency_ms"), e.get("message"), e.get("trace_id"), json.dumps(e.get("attrs") or {}),
         bool(e.get("is_error")))
        for e in events
    ]
    async with app.state.pool.acquire() as conn:
        await conn.executemany(INSERT_SQL, rows)
    await windows.record(app.state.redis, windows.aggregate_events(events), received.timestamp(), settings.bucket_sec)
    EVENTS_PROCESSED.inc(len(events))


async def consume(app: FastAPI) -> None:
    queue = app.state.queues[mq.Q_RAW]

    async def on_message(message):
        # requeue=False -> poison messages go to the dead-letter queue instead of looping forever
        async with message.process(requeue=False):
            with BATCH_SECONDS.time():
                await handle_batch(app, json.loads(message.body))

    await queue.consume(on_message)
    log.info("consuming", extra={"queue": mq.Q_RAW})


async def is_leader(redis, key: str, ttl: int) -> bool:
    return bool(await redis.eval(LEADER_LUA, 1, key, INSTANCE, ttl))


async def detector_loop(app: FastAPI) -> None:
    cfg, redis = detector_config(), app.state.redis
    interval = settings.detector_interval_sec
    while True:
        await asyncio.sleep(interval)
        try:
            leader = await is_leader(redis, "leader:detector", interval * 3)
            IS_LEADER.set(1 if leader else 0)
            if not leader:
                continue
            DETECTOR_TICKS.inc()
            now = time.time()
            services = sorted(await redis.smembers("services"))
            wins = await windows.load_window(redis, services, now, settings.bucket_sec, settings.window_buckets)
            for service in services:
                err_b, lat_b = await windows.load_baselines(redis, service)
                anomalies, err_b, lat_b = evaluate(cfg, service, wins[service], err_b, lat_b)
                await windows.save_baselines(redis, service, err_b, lat_b)
                last_seen = await redis.get(f"last_seen:{service}")
                silence = check_silence(cfg, service, now, float(last_seen) if last_seen else None)
                if silence:
                    anomalies.append(silence)
                for a in anomalies:
                    INCIDENTS_PUBLISHED.labels(a.kind).inc()
                    log.warning("anomaly detected", extra={"svc": service, "kind": a.kind, "severity": a.severity})
                    await mq.publish_json(app.state.exchange, mq.RK_INCIDENT, {
                        "fingerprint": f"{service}:{a.kind}", "service": service, "kind": a.kind,
                        "severity": a.severity, "summary": a.summary, "details": a.details,
                        "detected_at": datetime.utcnow().isoformat() + "Z",
                    })
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("detector tick failed")


async def retention_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(600)
        try:
            if await is_leader(app.state.redis, "leader:retention", 900):
                async with app.state.pool.acquire() as conn:
                    status = await conn.execute(
                        "DELETE FROM events WHERE ts < now() - make_interval(hours => $1)", settings.retention_hours
                    )
                log.info("retention sweep", extra={"result": status})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("retention failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(SERVICE, settings.log_level)
    app.state.pool = await db.create_pool(settings.postgres_dsn)
    await db.ensure_schema(app.state.pool)
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    app.state.connection = await mq.connect(settings.amqp_url)
    channel = await app.state.connection.channel()
    await channel.set_qos(prefetch_count=settings.prefetch)
    app.state.exchange, app.state.queues = await mq.declare_topology(channel)
    await consume(app)
    tasks = [asyncio.create_task(detector_loop(app)), asyncio.create_task(retention_loop(app))]
    log.info("processor started", extra={"instance": INSTANCE})
    yield
    for t in tasks:
        t.cancel()
    await app.state.connection.close()
    await app.state.pool.close()
    await app.state.redis.aclose()


app = FastAPI(title="SentinelOps Processor", lifespan=lifespan)


async def _pg_ready() -> bool:
    async with app.state.pool.acquire() as conn:
        return await conn.fetchval("SELECT 1") == 1


async def _redis_ready() -> bool:
    return bool(await app.state.redis.ping())


async def _mq_ready() -> bool:
    return not app.state.connection.is_closed


install_ops(app, SERVICE, {"postgres": _pg_ready, "redis": _redis_ready, "rabbitmq": _mq_ready})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_config=None)
