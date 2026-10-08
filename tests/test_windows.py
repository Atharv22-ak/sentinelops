from processor.windows import aggregate_events, bucket_of


def test_aggregate_events_groups_by_service():
    events = [
        {"service": "a", "is_error": True, "latency_ms": 100},
        {"service": "a", "is_error": False, "latency_ms": 300},
        {"service": "b", "is_error": False, "latency_ms": None},
    ]
    agg = aggregate_events(events)
    assert agg["a"].n == 2 and agg["a"].errors == 1 and agg["a"].mean_latency == 200
    assert agg["b"].n == 1 and agg["b"].lat_n == 0 and agg["b"].mean_latency == 0


def test_bucket_alignment():
    assert bucket_of(1234, 10) == 1230
    assert bucket_of(1230, 10) == 1230
