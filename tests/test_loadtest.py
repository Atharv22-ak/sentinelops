import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "loadtest.py"
spec = importlib.util.spec_from_file_location("loadtest", SCRIPT)
loadtest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loadtest)


def make(status, lat, conc=10, batch=100, duration=10):
    return loadtest.Result(concurrency=conc, batch=batch, duration=duration, latencies_ms=lat, status=status)


def test_percentile_nearest_rank():
    data = [float(i) for i in range(1, 101)]
    assert loadtest.percentile(data, 0.5) == 50.0
    assert loadtest.percentile(data, 0.95) == 95.0
    assert loadtest.percentile(data, 0.99) == 99.0
    assert loadtest.percentile([], 0.99) == 0.0
    assert loadtest.percentile([7.0], 0.99) == 7.0


def test_summary_rates_and_throughput():
    r = make({202: 90, 429: 6, 503: 2, "error": 2}, [10.0] * 90, batch=100, duration=10)
    s = loadtest.summarize(r)
    assert s["requests"] == 100
    assert s["success_rate_pct"] == 90.0
    assert s["rate_4xx_pct"] == 6.0
    assert s["rate_5xx_pct"] == 4.0  # 503 + transport errors
    assert s["requests_per_sec"] == 9.0
    assert s["events_per_sec"] == 900


def test_pass_when_within_thresholds():
    s = loadtest.summarize(make({202: 1000}, [20.0] * 1000))
    assert loadtest.evaluate(s, loadtest.Thresholds()) == []


def test_fail_on_each_violated_threshold():
    s = loadtest.summarize(make({202: 90, 500: 10}, [1500.0] * 90))
    failures = loadtest.evaluate(s, loadtest.Thresholds())
    joined = " | ".join(failures)
    assert "success rate" in joined and "5xx" in joined and "p95" in joined and "p99" in joined


def test_min_events_threshold_and_empty_run():
    s = loadtest.summarize(make({202: 10}, [5.0] * 10, batch=10, duration=10))
    assert loadtest.evaluate(s, loadtest.Thresholds(min_events_per_sec=1000))
    assert loadtest.evaluate(loadtest.summarize(make({}, [])), loadtest.Thresholds()) == ["no requests completed"]


def test_parse_int_list():
    assert loadtest.parse_int_list("10, 25,50,") == [10, 25, 50]
