"""Ops endpoints (/healthz /readyz /metrics) + HTTP metrics middleware shared by all services."""
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["app", "method", "path", "status"])
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["app", "method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)

ReadyCheck = Callable[[], Awaitable[bool]]


def install_ops(app: FastAPI, service: str, readiness: dict[str, ReadyCheck] | None = None) -> None:
    readiness = readiness or {}

    @app.middleware("http")
    async def _metrics(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        if path not in ("/metrics", "/healthz", "/readyz"):
            HTTP_REQUESTS.labels(service, request.method, path, response.status_code).inc()
            HTTP_LATENCY.labels(service, request.method, path).observe(time.perf_counter() - start)
        return response

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok", "service": service}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        results: dict[str, bool] = {}
        for name, check in readiness.items():
            try:
                results[name] = bool(await check())
            except Exception:  # noqa: BLE001
                results[name] = False
        ok = all(results.values())
        return JSONResponse({"ready": ok, "checks": results}, status_code=200 if ok else 503)

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
