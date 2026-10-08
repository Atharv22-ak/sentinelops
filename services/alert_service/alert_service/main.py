"""alert-service: de-duplicates incidents, persists them, notifies, and auto-resolves."""
import asyncio
import json
import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI
from prometheus_client import Counter

from sentinel_common import db, mq
from sentinel_common.config import get_settings
from sentinel_common.logging import setup_logging
from sentinel_common.web import install_ops

from .notifier import Notifier

SERVICE = "alert-service"
log = logging.getLogger(SERVICE)
settings = get_settings()

INCIDENTS = Counter("sentinel_incidents_total", "Incident messages handled", ["outcome"])
NOTIFICATIONS = Counter("sentinel_notifications_total", "Notifications sent", ["event", "ok"])

INSERT_INCIDENT = """
INSERT INTO incidents (fingerprint, service, kind, severity, summary, details)
VALUES ($1, $2, $3, $4, $5, $6::jsonb) RETURNING id, occurrences
"""
FIND_OPEN = "SELECT id FROM incidents WHERE fingerprint = $1 AND status <> 'resolved' ORDER BY opened_at DESC LIMIT 1"
BUMP = """
UPDATE incidents SET occurrences = occurrences + 1, last_seen_at = now(), severity = $2,
       summary = $3, details = $4::jsonb WHERE id = $1 RETURNING occurrences
"""
AUTO_RESOLVE = """
UPDATE incidents SET status = 'resolved', resolved_at = now()
WHERE status <> 'resolved' AND last_seen_at < now() - make_interval(secs => $1)
RETURNING id, service, kind, severity, summary, occurrences
"""


def _public(incident: dict, incident_id, occurrences: int) -> dict:
    base = settings.dashboard_url.rstrip("/")
    return {**incident, "id": str(incident_id), "occurrences": occurrences, "url": base or ""}


async def handle_incident(app: FastAPI, inc: dict) -> None:
    fp = inc["fingerprint"]
    details = json.dumps(inc.get("details", {}))
    async with app.state.pool.acquire() as conn:
        existing = await conn.fetchrow(FIND_OPEN, fp)
        if existing:
            occ = await conn.fetchval(BUMP, existing["id"], inc["severity"], inc["summary"], details)
            incident_id, event = existing["id"], "reminder"
            notify = bool(await app.state.redis.set(f"cooldown:{fp}", "1", nx=True, ex=settings.alert_cooldown_sec))
            INCIDENTS.labels("deduplicated").inc()
        else:
            row = await conn.fetchrow(INSERT_INCIDENT, fp, inc["service"], inc["kind"], inc["severity"],
                                      inc["summary"], details)
            incident_id, occ, event, notify = row["id"], row["occurrences"], "opened", True
            await app.state.redis.set(f"cooldown:{fp}", "1", ex=settings.alert_cooldown_sec)
            INCIDENTS.labels("opened").inc()
    if notify:
        ok = await app.state.notifier.send(_public(inc, incident_id, occ), event)
        NOTIFICATIONS.labels(event, str(ok).lower()).inc()


async def consume(app: FastAPI) -> None:
    async def on_message(message):
        async with message.process(requeue=False):
            await handle_incident(app, json.loads(message.body))

    await app.state.queues[mq.Q_INCIDENTS].consume(on_message)


async def auto_resolve_loop(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(30)
        try:
            async with app.state.pool.acquire() as conn:
                rows = await conn.fetch(AUTO_RESOLVE, float(settings.auto_resolve_sec))
            for r in rows:
                log.info("incident auto-resolved", extra={"incident_id": str(r["id"]), "kind": r["kind"]})
                ok = await app.state.notifier.send(_public(dict(r), r["id"], r["occurrences"]), "resolved")
                NOTIFICATIONS.labels("resolved", str(ok).lower()).inc()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("auto-resolve failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(SERVICE, settings.log_level)
    app.state.pool = await db.create_pool(settings.postgres_dsn)
    await db.ensure_schema(app.state.pool)
    app.state.redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    app.state.notifier = Notifier(settings.webhook_url, settings.webhook_kind)
    app.state.connection = await mq.connect(settings.amqp_url)
    channel = await app.state.connection.channel()
    await channel.set_qos(prefetch_count=10)
    app.state.exchange, app.state.queues = await mq.declare_topology(channel)
    await consume(app)
    task = asyncio.create_task(auto_resolve_loop(app))
    log.info("alert-service started", extra={"webhook": settings.webhook_kind if settings.webhook_url else "disabled"})
    yield
    task.cancel()
    await app.state.connection.close()
    await app.state.pool.close()
    await app.state.redis.aclose()


app = FastAPI(title="SentinelOps Alert Service", lifespan=lifespan)


async def _pg_ready() -> bool:
    async with app.state.pool.acquire() as conn:
        return await conn.fetchval("SELECT 1") == 1


async def _mq_ready() -> bool:
    return not app.state.connection.is_closed


install_ops(app, SERVICE, {"postgres": _pg_ready, "rabbitmq": _mq_ready})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_config=None)
