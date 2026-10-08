from processor.detector import Baseline, DetectorConfig, WindowStats, check_silence, evaluate, ewma_update, threshold

CFG = DetectorConfig()


def healthy(n=100):
    return WindowStats(n=n, errors=1, lat_sum=100.0 * n, lat_n=n)


def test_ewma_first_sample_initialises_baseline():
    b = ewma_update(Baseline(), 0.05, 0.1)
    assert (b.mean, b.var, b.n) == (0.05, 0.0, 1)


def test_ewma_converges_towards_new_level():
    b = Baseline()
    for _ in range(200):
        b = ewma_update(b, 0.10, 0.1)
    assert abs(b.mean - 0.10) < 1e-6


def test_threshold_uses_floor_during_warmup():
    assert threshold(Baseline(mean=0.01, var=0, n=2), 0.2, 3, 5) == 0.2


def test_threshold_adapts_after_warmup():
    base = Baseline(mean=0.3, var=0.01, n=50)  # std 0.1 -> 0.3 + 0.3 = 0.6
    assert abs(threshold(base, 0.2, 3, 5) - 0.6) < 1e-9


def test_no_anomaly_for_healthy_traffic():
    anomalies, eb, lb = evaluate(CFG, "api", healthy(), Baseline(), Baseline())
    assert anomalies == []
    assert eb.n == 1 and lb.n == 1  # baselines learned


def test_error_spike_detected_and_critical():
    stats = WindowStats(n=100, errors=60, lat_sum=10_000, lat_n=100)
    anomalies, eb, _ = evaluate(CFG, "api", stats, Baseline(), Baseline())
    kinds = {a.kind: a for a in anomalies}
    assert kinds["error_rate_spike"].severity == "critical"
    assert eb.n == 0  # anomalous window NOT learned


def test_latency_degradation_detected():
    stats = WindowStats(n=100, errors=0, lat_sum=100 * 3000.0, lat_n=100)
    anomalies, _, lb = evaluate(CFG, "api", stats, Baseline(), Baseline())
    assert [a.kind for a in anomalies] == ["latency_degradation"]
    assert anomalies[0].severity == "high"
    assert lb.n == 0


def test_too_few_events_is_ignored():
    stats = WindowStats(n=5, errors=5, lat_sum=50_000, lat_n=5)
    anomalies, eb, lb = evaluate(CFG, "api", stats, Baseline(), Baseline())
    assert anomalies == [] and eb.n == 0 and lb.n == 0


def test_noisy_service_gets_higher_threshold_than_floor():
    # a service that normally runs ~25% errors must not page at 30%
    base = Baseline(mean=0.25, var=0.0016, n=100)  # std 0.04 -> thr 0.37
    stats = WindowStats(n=100, errors=30, lat_sum=1000, lat_n=100)
    anomalies, _, _ = evaluate(CFG, "api", stats, base, Baseline())
    assert anomalies == []


def test_silence_detection_window():
    assert check_silence(CFG, "api", now=1000, last_seen=990) is None  # recent
    assert check_silence(CFG, "api", now=1000, last_seen=800).kind == "heartbeat_missing"
    assert check_silence(CFG, "api", now=10_000, last_seen=100) is None  # long gone, stop paging
    assert check_silence(CFG, "api", now=1000, last_seen=None) is None
