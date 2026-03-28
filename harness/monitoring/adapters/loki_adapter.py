"""
loki_adapter.py — Grafana Loki log source adapter.

Queries the Loki HTTP API (/loki/api/v1/query_range).
Compatible with Grafana Cloud Loki and self-hosted Loki.

Config (monitoring_config.yaml):
  adapters:
    loki:
      enabled: true
      url: https://logs-prod-us-central1.grafana.net
      username: "123456"          # Grafana Cloud: numeric org ID
      password: "${LOKI_API_KEY}" # Grafana Cloud: API key (use env var)
      query: '{service="myapp"}'  # LogQL label selector
      service: myapp
      timeout_seconds: 10
      max_lines_per_query: 500
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Iterator

from harness.monitoring.base_adapter import BaseLogAdapter
from harness.monitoring.log_event import LogEvent, LogLevel


class LokiAdapter(BaseLogAdapter):
    """Fetches log events from Grafana Loki via the HTTP query API."""

    SOURCE_NAME = "loki"

    def __init__(self, config: dict):
        super().__init__(config)
        self.url = config.get("url", "http://localhost:3100").rstrip("/")
        self.username = _resolve_env(config.get("username", ""))
        self.password = _resolve_env(config.get("password", ""))
        self.query = config.get("query", '{job=""}')
        self.service = config.get("service", "")
        self.timeout = config.get("timeout_seconds", 10)
        self.max_lines = config.get("max_lines_per_query", 500)

    def fetch(
        self,
        since: datetime,
        until: datetime,
        max_events: int = 500,
    ) -> list[LogEvent]:
        try:
            import urllib.request
            import urllib.parse
            import json
            import base64

            params = {
                "query": self.query,
                "start": str(int(since.timestamp() * 1e9)),  # nanoseconds
                "end": str(int(until.timestamp() * 1e9)),
                "limit": str(min(max_events, self.max_lines)),
                "direction": "FORWARD",
            }
            endpoint = f"{self.url}/loki/api/v1/query_range"
            url = f"{endpoint}?{urllib.parse.urlencode(params)}"

            req = urllib.request.Request(url)
            if self.username and self.password:
                creds = base64.b64encode(
                    f"{self.username}:{self.password}".encode()
                ).decode()
                req.add_header("Authorization", f"Basic {creds}")

            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())

            return self._parse_response(data)

        except Exception as exc:
            print(f"  ⚠ LokiAdapter fetch failed: {exc}")
            return []

    def stream(self) -> Iterator[LogEvent]:
        """
        Loki does not have a native push API from this adapter's perspective.
        Use polling via fetch_window() instead, or use the WebhookAdapter
        and configure Loki to push alerts to it.
        """
        raise NotImplementedError(
            "LokiAdapter does not support streaming. "
            "Use polling mode or configure WebhookAdapter as a Loki alert receiver."
        )

    def _parse_response(self, data: dict) -> list[LogEvent]:
        events = []
        result_type = data.get("data", {}).get("resultType", "")
        results = data.get("data", {}).get("result", [])

        for stream in results:
            labels = stream.get("stream", {})
            service = labels.get("service") or labels.get("app") or self.service

            for entry in stream.get("values", []):
                if len(entry) < 2:
                    continue
                ts_ns, line = entry[0], entry[1]
                ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc)
                level, message = _extract_level_from_line(line)
                events.append(LogEvent(
                    timestamp=ts,
                    level=level,
                    message=message,
                    source=self.SOURCE_NAME,
                    service=service,
                    host=labels.get("host", labels.get("pod", "")),
                    labels=labels,
                    raw=line,
                ))

        return sorted(events, key=lambda e: e.timestamp)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LEVEL_PATTERN = re.compile(
    r"\b(DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|FATAL)\b", re.IGNORECASE
)


def _extract_level_from_line(line: str) -> tuple[LogLevel, str]:
    """Extract log level from a raw log line, return (level, cleaned message)."""
    m = _LEVEL_PATTERN.search(line)
    if m:
        return LogLevel.from_string(m.group(1)), line
    # Try JSON
    if line.strip().startswith("{"):
        import json
        try:
            d = json.loads(line)
            level_str = d.get("level") or d.get("severity") or d.get("lvl") or "UNKNOWN"
            msg = d.get("message") or d.get("msg") or line
            return LogLevel.from_string(level_str), msg
        except Exception:
            pass
    return LogLevel.UNKNOWN, line


def _resolve_env(value: str) -> str:
    """Resolve ${ENV_VAR} placeholders in config values."""
    if value.startswith("${") and value.endswith("}"):
        env_key = value[2:-1]
        return os.environ.get(env_key, "")
    return value
