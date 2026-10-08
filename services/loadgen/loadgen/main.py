"""loadgen: simulates a fleet of microservices (with periodic injected faults) so the
platform can be demoed and load-tested end to end."""
import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI
from prometheus_client import Counter

from sentinel_common.config import get_settings
from sentinel_common.logging import setup_logging
from sentinel_common.web import install_ops

SERVICE = "loadgen"
log = logging.getLogger(SERVICE)
settings = get_settings()
SENT = Counter("loadgen_events_sent_total", "Events sent", ["service"])
FAULTS = Counter("loadgen_faults_injected_total", "Faults injected", ["kind"])

PROFILES = {  # service -> (base latency ms, base error rate)
    "checkout": (120, 0.01), "payments": (220, 0.015), "search": (60, 0.005),
    "auth": (40, 0.005), "inventory": (90, 0.01),
}
ENDPOINTS = ["/v1/items", "/v1/cart", "/v1/pay", "/v1/login", "/v1/search"]


def make_event(service: str, fault: dict | None) -> dict:
    base_lat, base_err = PROFILES[service]
    err_rate, lat_mult = base_err, 1.0
    if fault and fault["service"] == service:
        err_rate = fault.get("error_rate", base_err)
        lat_mult = fault.get("latency_mult", 1.0)
    failed = random.random() < err_rate
    latency = max(1.0, random.gauss(base_lat, base_lat * 0.2)) * lat_mult
    status = random.choice([500, 502, 503]) if failed else 200
    return {
        "service": service,
        "level": "ERROR" if failed else "INFO",
        "message": "upstream failure" if failed else "request ok",
        "latency_ms": round(latency, 1),
        "status_code": status,
        "attrs": {"endpoint": random.choice(ENDPOINTS), "region": "ap-south-1"},
    }


async def run(stop: asyncio.Event) -> None:
    fault: dict | None = None
    fault_until = 0.0
    next_fault = time.time() + 60
    async with httpx.AsyncClient(base_url=settings.ingest_url, headers={"X-API-Key": settings.ingest_api_key},
                                 timeout=10) as client:
        while not stop.is_set():
            now = time.time()
            if fault and now > fault_until:
                log.info("fault cleared", extra={"fault": fault})
                fault = None
            if not fault and now > next_fault:
                kind = random.choice(["errors", "latency"])
                fault = {"service": random.choice(list(PROFILES)),
                         **({"error_rate": 0.6} if kind == "errors" else {"latency_mult": 8.0})}
                fault_until = now + settings.fault_duration_sec
                next_fault = now + settings.fault_every_sec
                FAULTS.labels(kind).inc()
                log.warning("fault injected", extra={"fault": fault, "kind": kind})
            batch = [make_event(random.choice(list(PROFILES)), fault) for _ in range(settings.events_per_second)]
            try:
                resp = await client.post("/v1/events", json={"events": batch})
                if resp.status_code == 202:
                    for e in batch:
                        SENT.labels(e["service"]).inc()
                else:
                    log.warning("ingest rejected batch", extra={"status": resp.status_code})
            except httpx.HTTPError as exc:
                log.warning("ingest unreachable", extra={"error": str(exc)})
            await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(SERVICE, settings.log_level)
    stop = asyncio.Event()
    task = asyncio.create_task(run(stop))
    log.info("loadgen started", extra={"target": settings.ingest_url, "eps": settings.events_per_second})
    yield
    stop.set()
    task.cancel()


app = FastAPI(title="SentinelOps Load Generator", lifespan=lifespan)
install_ops(app, SERVICE)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_config=None)
