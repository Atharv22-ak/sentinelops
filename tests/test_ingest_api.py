import json

import pytest
from fastapi.testclient import TestClient

from ingest_api import main
from sentinel_common.config import Settings


class FakeRedis:
    def __init__(self):
        self.counts = {}

    async def incrby(self, key, n):
        self.counts[key] = self.counts.get(key, 0) + n
        return self.counts[key]

    async def expire(self, *_):
        return True

    async def ping(self):
        return True


class FakeExchange:
    def __init__(self):
        self.published = []

    async def publish(self, message, routing_key):
        self.published.append((routing_key, json.loads(message.body)))


@pytest.fixture
def client():
    app = main.app
    app.state.settings = Settings(api_keys="k1,k2", rate_limit_per_minute=10, max_batch_size=5)
    app.state.redis = FakeRedis()
    app.state.exchange = FakeExchange()
    return TestClient(app)  # no context manager -> lifespan (real RabbitMQ) is skipped


def post(client, n=1, key="k1", **kw):
    events = [{"service": "api", "level": "info", "latency_ms": 12, **kw} for _ in range(n)]
    headers = {"X-API-Key": key} if key else {}
    return client.post("/v1/events", json={"events": events}, headers=headers)


def test_missing_or_wrong_key_is_401(client):
    assert post(client, key=None).status_code == 401
    assert post(client, key="nope").status_code == 401


def test_valid_batch_is_accepted_and_published(client):
    r = post(client, n=3)
    assert r.status_code == 202 and r.json()["accepted"] == 3
    rk, body = client.app.state.exchange.published[0]
    assert rk == "event.raw" and len(body["events"]) == 3
    ev = body["events"][0]
    assert ev["event_id"] and ev["ts"] and ev["is_error"] is False


def test_5xx_marked_as_error(client):
    post(client, status_code=503)
    assert client.app.state.exchange.published[0][1]["events"][0]["is_error"] is True


def test_validation_error_is_422(client):
    r = client.post("/v1/events", json={"events": [{"service": "bad name"}]}, headers={"X-API-Key": "k1"})
    assert r.status_code == 422


def test_batch_too_large_is_413(client):
    assert post(client, n=6).status_code == 413


def test_rate_limit_returns_429_with_retry_after(client):
    assert post(client, n=5).status_code == 202
    assert post(client, n=5).status_code == 202
    r = post(client, n=1)
    assert r.status_code == 429 and "retry-after" in r.headers


def test_rate_limit_is_per_key(client):
    for _ in range(2):
        post(client, n=5, key="k1")
    assert post(client, n=1, key="k1").status_code == 429
    assert post(client, n=1, key="k2").status_code == 202
