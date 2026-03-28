"""
datadog_adapter.py — Datadog Logs and Events API adapter.

Queries the Datadog Logs API v2 (/api/v2/logs/events/search).
Compatible with all Datadog sites (US1, US3, US5, EU, AP1).

Config (monitoring_config.yaml):
  adapters:
    datadog:
      enabled: true
      site: datadoghq.com          # or datadoghq.eu, us3.datadoghq.com, etc.
      api_key: "${DD_API_KEY}"
      app_key: "${DD_APP_KEY}"
      query: "service:myapp status:error"   # Datadog log query syntax
      service: myapp
      timeout_seconds: 15
      max_events_per_query: 500
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Iterator

from harness.monitoring.base_adapter import BaseLogAdapter
from harness.monitoring.log_event import LogEvent, LogLevel


class DatadogAdapter(BaseLogAdapter):
    """Fetches log events from Datadog via the Logs Search API v2."""

    SOURCE_NAME = "datadog"

    def __init__(self, config: dict):
        super().__init__(config)
        self.site = config.get("site", "datadoghq.com")
        self.api_key = _resolve_env(config.get("api_key", ""))
        self.app_key = _resolve_env(config.get("app_key", ""))
        self.query = config.get("query", "")
        self.service = config.get("service", "")
        self.timeout = config.get("timeout_seconds", 15)
        self.max_events = config.get("max_events_per_query", 500)
        self.base_url = f"https://api.{self.site}"

    def fetch(
        self,
        since: datetime,
        until: datetime,
        max_events: int = 500,
    ) -> list[LogEvent]:
        try:
            payload = {
                "filter": {
                    "query": self.query,
                    "from": since.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                    "to": until.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
                },
                "sort": "timestamp",
                "page": {"limit": min(max_events, self.max_events)},
            }

            url = f"{self.base_url}/api/v2/logs/events/search"
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "DD-API-KEY": self.api_key,
                    "DD-APPLICATION-KEY": self.app_key,
                },
            )

            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())

            return self._parse_response(data)

        except Exception as exc:
            print(f"  ⚠ DatadogAdapter fetch failed: {exc}")
            return []

    def stream(self) -> Iterator[LogEvent]:
        """
        Datadog does not support log streaming from this adapter.
        Use polling mode. For real-time alerts, configure a Datadog
        monitor webhook pointing to the WebhookAdapter.
        """
        raise NotImplementedError(
            "DatadogAdapter does not support streaming. "
            "Use polling mode or configure a Datadog webhook monitor."
        )

    def _parse_response(self, data: dict) -> list[LogEvent]:
        events = []
        for log in data.get("data", []):
            attrs = log.get("attributes", {})
            ts_str = attrs.get("timestamp", "")
            ts = _parse_iso(ts_str) or datetime.now(timezone.utc)

            status = attrs.get("status", "info")
            level = _dd_status_to_level(status)

            message = attrs.get("message", "")
            service = attrs.get("service") or self.service
            host = attrs.get("host", "")

            # Flatten tags into labels
            tags = attrs.get("tags", [])
            labels = {}
            for tag in tags:
                if ":" in tag:
                    k, v = tag.split(":", 1)
                    labels[k] = v
                else:
                    labels[tag] = "true"

            events.append(LogEvent(
                timestamp=ts,
                level=level,
                message=message,
                source=self.SOURCE_NAME,
                service=service,
                host=host,
                trace_id=attrs.get("dd", {}).get("trace_id", ""),
                span_id=attrs.get("dd", {}).get("span_id", ""),
                labels=labels,
                raw=json.dumps(attrs),
            ))

        return sorted(events, key=lambda e: e.timestamp)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dd_status_to_level(status: str) -> LogLevel:
    mapping = {
        "emerg": LogLevel.CRITICAL,
        "alert": LogLevel.CRITICAL,
        "critical": LogLevel.CRITICAL,
        "error": LogLevel.ERROR,
        "warn": LogLevel.WARNING,
        "warning": LogLevel.WARNING,
        "notice": LogLevel.INFO,
        "info": LogLevel.INFO,
        "debug": LogLevel.DEBUG,
        "trace": LogLevel.DEBUG,
    }
    return mapping.get(status.lower(), LogLevel.UNKNOWN)


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _resolve_env(value: str) -> str:
    if value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value
