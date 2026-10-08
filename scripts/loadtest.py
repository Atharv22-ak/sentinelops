"""Tiny async load test for the ingest API: measures real throughput and latency percentiles.

    python scripts/loadtest.py --url http://sentinel-ingest.3.108.88.140.nip.io:32136 --api-key KEY
"""
import argparse
import asyncio
import random
import statistics
import time

import httpx


def batch(n: int) -> dict:
    return {"events": [{"service": random.choice(["lt-a", "lt-b", "lt-c"]), "level": "info",
                        "latency_ms": random.uniform(5, 80), "status_code": 200} for _ in range(n)]}


async def worker(client, args, stop_at, lat, counts):
    while time.time() < stop_at:
        t0 = time.perf_counter()
        try:
            r = await client.post("/v1/events", json=batch(args.batch))
            counts[r.status_code] = counts.get(r.status_code, 0) + 1
            if r.status_code == 202:
                lat.append((time.perf_counter() - t0) * 1000)
        except httpx.HTTPError:
            counts["error"] = counts.get("error", 0) + 1


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--duration", type=int, default=30)
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--batch", type=int, default=100)
    args = ap.parse_args()
    lat: list[float] = []
    counts: dict = {}
    stop_at = time.time() + args.duration
    async with httpx.AsyncClient(base_url=args.url, headers={"X-API-Key": args.api_key}, timeout=15) as client:
        await asyncio.gather(*(worker(client, args, stop_at, lat, counts) for _ in range(args.concurrency)))
    ok = counts.get(202, 0)
    print(f"responses: {counts}")
    if lat:
        lat.sort()
        q = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))]  # noqa: E731
        print(f"requests/s : {ok / args.duration:,.1f}")
        print(f"events/s   : {ok * args.batch / args.duration:,.0f}")
        print(f"latency ms : p50={q(.5):.1f} p95={q(.95):.1f} p99={q(.99):.1f} mean={statistics.mean(lat):.1f}")


if __name__ == "__main__":
    asyncio.run(main())
