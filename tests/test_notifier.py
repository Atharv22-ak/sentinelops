import httpx
import pytest

from alert_service.notifier import Notifier, format_slack, format_teams

INC = {"service": "payments", "kind": "error_rate_spike", "severity": "critical",
       "summary": "payments: error rate 60%", "occurrences": 2, "id": "abc", "url": "http://x"}


def test_teams_card_is_adaptive_card_with_facts_and_action():
    payload = format_teams(INC, "opened")
    att = payload["attachments"][0]
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = att["content"]
    assert card["type"] == "AdaptiveCard"
    assert {"title": "Service", "value": "payments"} in card["body"][2]["facts"]
    assert card["actions"][0]["url"] == "http://x"


def test_slack_text_contains_severity():
    assert "[CRITICAL]" in format_slack(INC, "opened")["text"]


@pytest.mark.asyncio
async def test_notifier_posts_and_retries_on_failure(monkeypatch):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        return httpx.Response(500 if len(calls) == 1 else 200)

    async def no_sleep(_):
        return None

    monkeypatch.setattr("alert_service.notifier.asyncio.sleep", no_sleep)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await Notifier("http://hook", "teams", client).send(INC, "opened") is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_notifier_disabled_without_url():
    assert await Notifier("", "teams").send(INC, "opened") is False
