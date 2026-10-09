"""Async load test for the ingest API with pass/fail thresholds (exit code 1 when a threshold is missed).

Single run (backwards compatible):
    python scripts/loadtest.py --url http://sentinel-ingest.3.108.88.140.nip.io:32136 --api-key KEY

Progressive ramp (recommended; each stage must pass before the next one starts):
    python scripts/loadtest.py --url ... --api-key KEY --stages 10,25,50,100 --batches 100,500 --duration 60

Longer soak after the smoke tests pass:
    python scripts/loadtest.py --url ... --api-key KEY --concurrency 25 --batch 100 --duration 300

Reported per run: HTTP success rate, 4xx / 5xx / transport-error rates, requests/s, events/s, p50/p95/p99 latency.

Before a *capacity* test raise the guard rails, otherwise you measure the limiter, not the platform:
    RATE_LIMIT_PER_MINUTE (default 6000 events/min/key = 100 events/s -> HTTP 429)
    MAX_BATCH_SIZE        (default 500 -> a batch of 1000 gets HTTP 413)
    make loadtest-prep    # raises both temporarily;  make loadtest-restore  # puts the chart defaults back
"""
import argparse
import asyncio
import json
import math
import random
import sys
import time
from dataclasses import dataclass, field

import httpx


@dataclass
class Thresholds:
    min_success_rate: float = 99.0  # % of requests answered 202
    max_5xx_rate: float = 1.0  # % of requests
    max_p95_ms: float = 500.0
    max_p99_ms: float = 1000.0
    min_events_per_sec: float = 0.0  # 0 = not enforced


@dataclass
class Result:
    concurrency: int
    batch: int
    duration: float
    latencies_ms: list[float] = field(default_factory=list)
    status: dict = field(default_factory=dict)  # int status code or "error" -> count

    @property
    def total(self) -> int:
        return sum(self.status.values())

    @property
    def ok(self) -> int:
        return self.status.get(202, 0)


def make_batch(n: int) -> dict:
    return {"events": [{"service": random.choice(["lt-a", "lt-b", "lt-c"]), "level": "info",
                        "latency_ms": random.uniform(5, 80), "status_code": 200} for _ in range(n)]}


def percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile on an ascending list; 0.0 for an empty list."""
    if not sorted_values:
        return 0.0
    rank = max(1, math.ceil(p * len(sorted_values)))
    return sorted_values[min(len(sorted_values), rank) - 1]


def _rate(count: int, total: int) -> float:
    return 100.0 * count / total if total else 0.0


def summarize(r: Result) -> dict:
    lat = sorted(r.latencies_ms)
    c4 = sum(v for k, v in r.status.items() if isinstance(k, int) and 400 <= k < 500)
    c5 = sum(v for k, v in r.status.items() if isinstance(k, int) and k >= 500)
    err = r.status.get("error", 0)
    return {
        "concurrency": r.concurrency,
        "batch": r.batch,
        "duration_s": r.duration,
        "requests": r.total,
        "success_rate_pct": round(_rate(r.ok, r.total), 2),
        "rate_4xx_pct": round(_rate(c4, r.total), 2),
        "rate_5xx_pct": round(_rate(c5 + err, r.total), 2),  # transport errors count as server-side failures
        "rate_429_pct": round(_rate(r.status.get(429, 0), r.total), 2),
        "requests_per_sec": round(r.ok / r.duration, 1) if r.duration else 0.0,
        "events_per_sec": round(r.ok * r.batch / r.duration) if r.duration else 0,
        "p50_ms": round(percentile(lat, 0.50), 1),
        "p95_ms": round(percentile(lat, 0.95), 1),
        "p99_ms": round(percentile(lat, 0.99), 1),
        "mean_ms": round(sum(lat) / len(lat), 1) if lat else 0.0,
        "status_counts": {str(k): v for k, v in sorted(r.status.items(), key=lambda kv: str(kv[0]))},
    }


def evaluate(s: dict, t: Thresholds) -> list[str]:
    """Return a list of human readable threshold violations (empty list == PASS)."""
    failures = []
    if s["requests"] == 0:
        return ["no requests completed"]
    if s["success_rate_pct"] < t.min_success_rate:
        failures.append(f"success rate {s['success_rate_pct']}% < {t.min_success_rate}%")
    if s["rate_5xx_pct"] > t.max_5xx_rate:
        failures.append(f"5xx/error rate {s['rate_5xx_pct']}% > {t.max_5xx_rate}%")
    if s["p95_ms"] > t.max_p95_ms:
        failures.append(f"p95 {s['p95_ms']}ms > {t.max_p95_ms}ms")
    if s["p99_ms"] > t.max_p99_ms:
        failures.append(f"p99 {s['p99_ms']}ms > {t.max_p99_ms}ms")
    if t.min_events_per_sec and s["events_per_sec"] < t.min_events_per_sec:
        failures.append(f"events/s {s['events_per_sec']} < {t.min_events_per_sec}")
    return failures


async def worker(client: httpx.AsyncClient, batch: int, stop_at: float, res: Result) -> None:
    while time.time() < stop_at:
        t0 = time.perf_counter()
        try:
            r = await client.post("/v1/events", json=make_batch(batch))
            res.status[r.status_code] = res.status.get(r.status_code, 0) + 1
            if r.status_code == 202:
                res.latencies_ms.append((time.perf_counter() - t0) * 1000)
        except httpx.HTTPError:
            res.status["error"] = res.status.get("error", 0) + 1


async def run_stage(url: str, api_key: str, concurrency: int, batch: int, duration: int) -> Result:
    res = Result(concurrency=concurrency, batch=batch, duration=duration)
    stop_at = time.time() + duration
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=url, headers={"X-API-Key": api_key}, timeout=15, limits=limits) as client:
        await asyncio.gather(*(worker(client, batch, stop_at, res) for _ in range(concurrency)))
    return res


def print_summary(s: dict, failures: list[str]) -> None:
    print(f"\n=== concurrency={s['concurrency']} batch={s['batch']} duration={s['duration_s']}s ===")
    print(f"responses  : {s['status_counts']}")
    print(f"success    : {s['success_rate_pct']}%   4xx: {s['rate_4xx_pct']}%   5xx+errors: {s['rate_5xx_pct']}%")
    print(f"requests/s : {s['requests_per_sec']:,.1f}")
    print(f"events/s   : {s['events_per_sec']:,}")
    print(f"latency ms : p50={s['p50_ms']} p95={s['p95_ms']} p99={s['p99_ms']} mean={s['mean_ms']}")
    if s["rate_429_pct"] > 0:
        print("hint       : HTTP 429 seen - rate limiter active (raise RATE_LIMIT_PER_MINUTE: make loadtest-prep)")
    if s["status_counts"].get("413"):
        print("hint       : HTTP 413 seen - batch exceeds MAX_BATCH_SIZE (see make loadtest-prep)")
    print("RESULT     : " + ("PASS" if not failures else "FAIL - " + "; ".join(failures)))


def parse_int_list(text: str) -> list[int]:
    return [int(x) for x in text.split(",") if x.strip()]


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--duration", type=int, default=30, help="seconds per stage (use 300 for a 5 minute soak)")
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--stages", help="comma list of concurrency levels, e.g. 10,25,50,100 (overrides --concurrency)")
    ap.add_argument("--batches", help="comma list of batch sizes, e.g. 100,500,1000 (overrides --batch)")
    ap.add_argument("--keep-going", action="store_true", help="do not stop the ramp after the first failed stage")
    ap.add_argument("--json", dest="json_out", help="write all stage summaries to this file")
    d = Thresholds()
    ap.add_argument("--min-success-rate", type=float, default=d.min_success_rate, help="%% of requests answered 202")
    ap.add_argument("--max-5xx-rate", type=float, default=d.max_5xx_rate, help="%% of 5xx + transport errors")
    ap.add_argument("--max-p95-ms", type=float, default=d.max_p95_ms)
    ap.add_argument("--max-p99-ms", type=float, default=d.max_p99_ms)
    ap.add_argument("--min-events-per-sec", type=float, default=d.min_events_per_sec)
    args = ap.parse_args(argv)

    thresholds = Thresholds(args.min_success_rate, args.max_5xx_rate, args.max_p95_ms, args.max_p99_ms,
                            args.min_events_per_sec)
    stages = parse_int_list(args.stages) if args.stages else [args.concurrency]
    batches = parse_int_list(args.batches) if args.batches else [args.batch]

    print(f"thresholds : {thresholds}")
    summaries, all_ok = [], True
    for batch in batches:
        for conc in stages:
            res = await run_stage(args.url, args.api_key, conc, batch, args.duration)
            s = summarize(res)
            failures = evaluate(s, thresholds)
            s["passed"] = not failures
            s["failures"] = failures
            summaries.append(s)
            print_summary(s, failures)
            if failures:
                all_ok = False
                if not args.keep_going:
                    print("\nstopping the ramp: fix the bottleneck before going higher (--keep-going to continue)")
                    break
        if not all_ok and not args.keep_going:
            break

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump({"thresholds": thresholds.__dict__, "stages": summaries, "passed": all_ok}, fh, indent=2)
    print("\nOVERALL    : " + ("PASS" if all_ok else "FAIL"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
