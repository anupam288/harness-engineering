"""
webhook_adapter.py — HTTP webhook receiver adapter.

The application (or Grafana Alert Manager, Datadog Monitor, etc.)
POSTs log events to this adapter. The adapter buffers them in memory
and serves them via fetch().

Supports two payload formats:
  - Generic JSON:  {"level": "error", "message": "...", "timestamp": "..."}
  - Alertmanager:  Grafana/Prometheus AlertManager webhook format
  - Datadog:       Datadog monitor webhook format

Config (monitoring_config.yaml):
  adapters:
    webhook:
      enabled: true
      host: 0.0.0.0
      port: 9876
      path: /harness/logs
      secret: "${WEBHOOK_SECRET}"   # optional HMAC-SHA256 verification
      max_buffer_size: 10000        # max events to keep in memory
      format: auto                  # auto | generic | alertmanager | datadog
      service: myapp

Start the webhook server:
  python cli.py monitor --adapter webhook --serve
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Iterator

from harness.monitoring.base_adapter import BaseLogAdapter
from harness.monitoring.log_event import LogEvent, LogLevel


class WebhookAdapter(BaseLogAdapter):
    """
    Receives log events pushed by external systems via HTTP POST.
    Buffers events in a thread-safe deque. fetch() drains the buffer.
    """

    SOURCE_NAME = "webhook"

    def __init__(self, config: dict):
        super().__init__(config)
        self.host = config.get("host", "0.0.0.0")
        self.port = config.get("port", 9876)
        self.path = config.get("path", "/harness/logs")
        self.secret = _resolve_env(config.get("secret", ""))
        self.max_buffer = config.get("max_buffer_size", 10_000)
        self.fmt = config.get("format", "auto")
        self.service = config.get("service", "")
        self._buffer: deque[LogEvent] = deque(maxlen=self.max_buffer)
        self._lock = threading.Lock()
        self._server: HTTPServer | None = None

    # ------------------------------------------------------------------
    # BaseLogAdapter implementation
    # ------------------------------------------------------------------

    def fetch(
        self,
        since: datetime,
        until: datetime,
        max_events: int = 500,
    ) -> list[LogEvent]:
        """Return buffered events within the time range."""
        with self._lock:
            events = [
                e for e in self._buffer
                if since <= e.timestamp <= until
            ]
        return sorted(events, key=lambda e: e.timestamp)[:max_events]

    def stream(self) -> Iterator[LogEvent]:
        """
        Yield events as they arrive. Starts the HTTP server if not running.
        Blocks until the server is stopped.
        """
        self.start_server()
        # Yield events as they appear in the buffer
        last_seen = 0
        import time
        while True:
            with self._lock:
                buf_list = list(self._buffer)
            for event in buf_list[last_seen:]:
                yield event
            last_seen = len(buf_list)
            time.sleep(0.1)

    # ------------------------------------------------------------------
    # HTTP server management
    # ------------------------------------------------------------------

    def start_server(self, daemon: bool = True) -> None:
        """Start the webhook HTTP server in a background thread."""
        if self._server is not None:
            return

        adapter = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != adapter.path:
                    self.send_response(404)
                    self.end_headers()
                    return

                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)

                # Optional HMAC verification
                if adapter.secret:
                    sig = self.headers.get("X-Harness-Signature", "")
                    if not adapter._verify_hmac(body, sig):
                        self.send_response(401)
                        self.end_headers()
                        return

                events = adapter._parse_payload(body)
                with adapter._lock:
                    adapter._buffer.extend(events)

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps({"received": len(events)}).encode()
                )

            def log_message(self, fmt, *args):
                pass  # suppress default HTTP server logs

        self._server = HTTPServer((self.host, self.port), Handler)
        t = threading.Thread(target=self._server.serve_forever, daemon=daemon)
        t.start()
        print(f"  ✓ WebhookAdapter listening on {self.host}:{self.port}{self.path}")

    def stop_server(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None

    def push(self, payload: bytes) -> list[LogEvent]:
        """Directly push a payload (for testing without HTTP)."""
        events = self._parse_payload(payload)
        with self._lock:
            self._buffer.extend(events)
        return events

    # ------------------------------------------------------------------
    # Payload parsers
    # ------------------------------------------------------------------

    def _parse_payload(self, body: bytes) -> list[LogEvent]:
        try:
            data = json.loads(body)
        except Exception:
            return [LogEvent(
                timestamp=datetime.now(timezone.utc),
                level=LogLevel.UNKNOWN,
                message=body.decode("utf-8", errors="replace"),
                source=self.SOURCE_NAME,
                service=self.service,
                raw=body.decode("utf-8", errors="replace"),
            )]

        fmt = self.fmt
        if fmt == "auto":
            fmt = self._detect_format(data)

        if fmt == "alertmanager":
            return self._parse_alertmanager(data)
        if fmt == "datadog":
            return self._parse_datadog_webhook(data)
        return self._parse_generic(data)

    def _detect_format(self, data: dict | list) -> str:
        if isinstance(data, dict):
            if "alerts" in data and "commonLabels" in data:
                return "alertmanager"
            if "event_type" in data and "aggreg_key" in data:
                return "datadog"
        return "generic"

    def _parse_generic(self, data: dict | list) -> list[LogEvent]:
        if isinstance(data, list):
            return [self._parse_single_generic(item) for item in data]
        return [self._parse_single_generic(data)]

    def _parse_single_generic(self, d: dict) -> LogEvent:
        ts_str = d.get("timestamp") or d.get("ts") or d.get("time") or ""
        ts = _parse_iso(ts_str) or datetime.now(timezone.utc)
        level = LogLevel.from_string(
            d.get("level") or d.get("severity") or d.get("lvl") or "UNKNOWN"
        )
        message = d.get("message") or d.get("msg") or d.get("text") or str(d)
        return LogEvent(
            timestamp=ts, level=level, message=message,
            source=self.SOURCE_NAME,
            service=d.get("service", self.service),
            host=d.get("host", ""),
            trace_id=d.get("trace_id", ""),
            labels={k: str(v) for k, v in d.items()
                    if k not in ("timestamp", "ts", "time", "level", "message")},
            raw=json.dumps(d),
        )

    def _parse_alertmanager(self, data: dict) -> list[LogEvent]:
        """Parse Grafana AlertManager / Prometheus Alertmanager webhook format."""
        events = []
        for alert in data.get("alerts", []):
            status = alert.get("status", "firing")
            level = LogLevel.CRITICAL if status == "firing" else LogLevel.INFO
            labels = alert.get("labels", {})
            annotations = alert.get("annotations", {})
            message = (
                annotations.get("description") or
                annotations.get("summary") or
                labels.get("alertname", "Unknown alert")
            )
            ts_str = alert.get("startsAt", "")
            ts = _parse_iso(ts_str) or datetime.now(timezone.utc)
            events.append(LogEvent(
                timestamp=ts, level=level, message=message,
                source=self.SOURCE_NAME,
                service=labels.get("service", self.service),
                host=labels.get("instance", ""),
                labels={**labels, "status": status, "alertname": labels.get("alertname", "")},
                raw=json.dumps(alert),
            ))
        return events

    def _parse_datadog_webhook(self, data: dict) -> list[LogEvent]:
        """Parse Datadog monitor webhook format."""
        ts = datetime.now(timezone.utc)
        level = LogLevel.ERROR if "error" in data.get("alert_type", "").lower() else LogLevel.WARNING
        message = data.get("title") or data.get("text") or "Datadog alert"
        return [LogEvent(
            timestamp=ts, level=level, message=message,
            source=self.SOURCE_NAME,
            service=data.get("tags", {}).get("service", self.service),
            host=data.get("host", ""),
            labels={"alert_type": data.get("alert_type", ""),
                    "event_type": data.get("event_type", "")},
            raw=json.dumps(data),
        )]

    def _verify_hmac(self, body: bytes, signature: str) -> bool:
        expected = hmac.new(
            self.secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature.lstrip("sha256="))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
