"""Notification formatting + delivery (generic JSON, Slack, Microsoft Teams)."""
from __future__ import annotations

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

SEVERITY_COLOR = {"critical": "attention", "high": "warning", "medium": "accent", "low": "default"}


def format_generic(incident: dict, event: str) -> dict:
    return {"event": event, **incident}


def format_slack(incident: dict, event: str) -> dict:
    icon = {"opened": ":rotating_light:", "reminder": ":bell:", "resolved": ":white_check_mark:"}.get(event, "")
    return {
        "text": f"{icon} *[{incident['severity'].upper()}] {incident['kind']}* ({event})\n"
                f"{incident['summary']}\n<{incident.get('url', '')}|Open dashboard>"
    }


def format_teams(incident: dict, event: str) -> dict:
    """Adaptive Card payload for a Microsoft Teams (Workflows) incoming webhook."""
    title = {"opened": "Incident opened", "reminder": "Incident still firing", "resolved": "Incident resolved"}[event]
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.4",
        "body": [
            {"type": "TextBlock", "size": "Large", "weight": "Bolder",
             "text": f"{title}: {incident['kind']}",
             "color": "Good" if event == "resolved" else "Attention"},
            {"type": "TextBlock", "wrap": True, "text": incident["summary"]},
            {"type": "FactSet", "facts": [
                {"title": "Service", "value": incident["service"]},
                {"title": "Severity", "value": incident["severity"]},
                {"title": "Occurrences", "value": str(incident.get("occurrences", 1))},
                {"title": "Incident", "value": str(incident.get("id", ""))},
            ]},
        ],
    }
    if incident.get("url"):
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open dashboard", "url": incident["url"]}]
    return {"type": "message", "attachments": [
        {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]}


FORMATTERS = {"generic": format_generic, "slack": format_slack, "teams": format_teams}


class Notifier:
    def __init__(self, url: str, kind: str = "generic", client: httpx.AsyncClient | None = None):
        self.url, self.kind = url, kind
        self.formatter = FORMATTERS.get(kind, format_generic)
        self.client = client or httpx.AsyncClient(timeout=10)

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def send(self, incident: dict, event: str) -> bool:
        if not self.enabled:
            log.info("webhook disabled, skipping notification", extra={"incident_kind": incident["kind"]})
            return False
        payload = self.formatter(incident, event)
        for attempt in range(1, 4):
            try:
                resp = await self.client.post(self.url, json=payload)
                if resp.status_code < 300:
                    return True
                log.warning("webhook non-2xx", extra={"status": resp.status_code, "attempt": attempt})
            except httpx.HTTPError as exc:
                log.warning("webhook error", extra={"error": str(exc), "attempt": attempt})
            await asyncio.sleep(2**attempt)
        return False
