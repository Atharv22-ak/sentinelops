"""Redis-backed sliding window: events are rolled into fixed time buckets."""
from __future__ import annotations

import time
from collections import defaultdict

from .detector import Baseline, WindowStats

BUCKET_TTL = 3600


def aggregate_events(events: list[dict]) -> dict[str, WindowStats]:
    agg: dict[str, WindowStats] = defaultdict(WindowStats)
    for ev in events:
        s = agg[ev["service"]]
        s.n += 1
        s.errors += 1 if ev.get("is_error") else 0
        if ev.get("latency_ms") is not None:
            s.lat_sum += float(ev["latency_ms"])
            s.lat_n += 1
    return dict(agg)


def bucket_of(ts: float, bucket_sec: int) -> int:
    return int(ts // bucket_sec) * bucket_sec


async def record(redis, agg: dict[str, WindowStats], ts: float, bucket_sec: int) -> None:
    bucket = bucket_of(ts, bucket_sec)
    pipe = redis.pipeline(transaction=False)
    for service, s in agg.items():
        key = f"b:{service}:{bucket}"
        pipe.hincrby(key, "n", s.n)
        pipe.hincrby(key, "errors", s.errors)
        pipe.hincrbyfloat(key, "lat_sum", s.lat_sum)
        pipe.hincrby(key, "lat_n", s.lat_n)
        pipe.expire(key, BUCKET_TTL)
        pipe.sadd("services", service)
        pipe.set(f"last_seen:{service}", time.time())
    await pipe.execute()


async def load_window(redis, services: list[str], now: float, bucket_sec: int, nbuckets: int) -> dict[str, WindowStats]:
    """Sum the last `nbuckets` COMPLETED buckets for each service."""
    current = bucket_of(now, bucket_sec)
    starts = [current - bucket_sec * i for i in range(1, nbuckets + 1)]
    pipe = redis.pipeline(transaction=False)
    for service in services:
        for start in starts:
            pipe.hgetall(f"b:{service}:{start}")
    raw = await pipe.execute()
    out: dict[str, WindowStats] = {}
    i = 0
    for service in services:
        total = WindowStats()
        for _ in starts:
            h = raw[i]
            i += 1
            if h:
                total = total.merge(
                    WindowStats(int(h.get("n", 0)), int(h.get("errors", 0)),
                                float(h.get("lat_sum", 0)), int(h.get("lat_n", 0)))
                )
        out[service] = total
    return out


async def load_baselines(redis, service: str) -> tuple[Baseline, Baseline]:
    data = await redis.hgetall(f"base:{service}")
    return Baseline.from_dict(data, "err"), Baseline.from_dict(data, "lat")


async def save_baselines(redis, service: str, err: Baseline, lat: Baseline) -> None:
    await redis.hset(f"base:{service}", mapping={**err.to_dict("err"), **lat.to_dict("lat")})
