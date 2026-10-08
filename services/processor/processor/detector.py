"""Pure anomaly-detection logic (no I/O) so it is trivially unit-testable.

Approach: per service we keep an exponentially-weighted moving mean/variance of the
error-rate and mean-latency measured over a sliding window. A window is anomalous when
it exceeds BOTH an absolute floor and (baseline mean + z * std). Anomalous windows are
not fed back into the baseline, so an outage can't teach the detector that it is normal.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class WindowStats:
    n: int = 0
    errors: int = 0
    lat_sum: float = 0.0
    lat_n: int = 0

    @property
    def error_rate(self) -> float:
        return self.errors / self.n if self.n else 0.0

    @property
    def mean_latency(self) -> float:
        return self.lat_sum / self.lat_n if self.lat_n else 0.0

    def merge(self, other: WindowStats) -> WindowStats:
        return WindowStats(
            self.n + other.n, self.errors + other.errors, self.lat_sum + other.lat_sum, self.lat_n + other.lat_n
        )


@dataclass
class Baseline:
    mean: float = 0.0
    var: float = 0.0
    n: int = 0

    @property
    def std(self) -> float:
        return math.sqrt(max(self.var, 0.0))

    def to_dict(self, prefix: str) -> dict[str, float]:
        return {f"{prefix}_mean": self.mean, f"{prefix}_var": self.var, f"{prefix}_n": self.n}

    @classmethod
    def from_dict(cls, data: dict, prefix: str) -> Baseline:
        return cls(
            float(data.get(f"{prefix}_mean", 0.0)),
            float(data.get(f"{prefix}_var", 0.0)),
            int(float(data.get(f"{prefix}_n", 0))),
        )


@dataclass(frozen=True)
class DetectorConfig:
    min_events: int = 20
    error_rate_floor: float = 0.2
    latency_floor_ms: float = 500.0
    z_threshold: float = 3.0
    alpha: float = 0.1
    warmup_samples: int = 5
    heartbeat_timeout_sec: int = 90


@dataclass
class Anomaly:
    kind: str
    severity: str
    summary: str
    details: dict = field(default_factory=dict)


def ewma_update(base: Baseline, x: float, alpha: float) -> Baseline:
    """Incremental exponentially-weighted mean/variance (West 1979)."""
    if base.n == 0:
        return Baseline(mean=x, var=0.0, n=1)
    diff = x - base.mean
    incr = alpha * diff
    mean = base.mean + incr
    var = (1 - alpha) * (base.var + diff * incr)
    return Baseline(mean=mean, var=var, n=base.n + 1)


def threshold(base: Baseline, floor: float, z: float, warmup: int) -> float:
    if base.n < warmup:
        return floor
    return max(floor, base.mean + z * base.std)


def evaluate(
    cfg: DetectorConfig, service: str, stats: WindowStats, err_base: Baseline, lat_base: Baseline
) -> tuple[list[Anomaly], Baseline, Baseline]:
    """Evaluate one window. Returns (anomalies, new_err_baseline, new_lat_baseline)."""
    anomalies: list[Anomaly] = []
    if stats.n < cfg.min_events:
        return anomalies, err_base, lat_base

    err_rate = stats.error_rate
    err_thr = threshold(err_base, cfg.error_rate_floor, cfg.z_threshold, cfg.warmup_samples)
    if err_rate > err_thr:
        sev = "critical" if err_rate >= 0.5 else "high"
        anomalies.append(
            Anomaly(
                "error_rate_spike",
                sev,
                f"{service}: error rate {err_rate:.0%} over last window (threshold {err_thr:.0%})",
                {"error_rate": round(err_rate, 4), "threshold": round(err_thr, 4),
                 "events": stats.n, "errors": stats.errors,
                 "baseline_mean": round(err_base.mean, 4)},
            )
        )
    else:
        err_base = ewma_update(err_base, err_rate, cfg.alpha)

    if stats.lat_n >= cfg.min_events:
        lat = stats.mean_latency
        lat_thr = threshold(lat_base, cfg.latency_floor_ms, cfg.z_threshold, cfg.warmup_samples)
        if lat > lat_thr:
            sev = "high" if lat >= 4 * lat_thr else "medium"
            anomalies.append(
                Anomaly(
                    "latency_degradation",
                    sev,
                    f"{service}: mean latency {lat:.0f} ms (threshold {lat_thr:.0f} ms)",
                    {"mean_latency_ms": round(lat, 1), "threshold_ms": round(lat_thr, 1),
                     "samples": stats.lat_n, "baseline_mean_ms": round(lat_base.mean, 1)},
                )
            )
        else:
            lat_base = ewma_update(lat_base, lat, cfg.alpha)
    return anomalies, err_base, lat_base


def check_silence(cfg: DetectorConfig, service: str, now: float, last_seen: float | None,
                  max_age_sec: int = 3600) -> Anomaly | None:
    """Heartbeat check: a service that was reporting recently but went quiet."""
    if last_seen is None:
        return None
    age = now - last_seen
    if cfg.heartbeat_timeout_sec < age <= max_age_sec:
        return Anomaly(
            "heartbeat_missing",
            "high",
            f"{service}: no events for {int(age)}s",
            {"seconds_silent": int(age)},
        )
    return None
