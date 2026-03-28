"""
file_adapter.py — File tail and stdout pipe adapter.

Reads log events from:
  - Local log files (tail -f style, or one-shot read)
  - Subprocess stdout/stderr pipes

Supports common log formats:
  - Plain text: "[2024-01-15 10:23:45] ERROR Something went wrong"
  - JSON: {"timestamp": "...", "level": "error", "message": "..."}
  - Logfmt: ts=2024-01-15 level=error msg="Something went wrong"

Config (monitoring_config.yaml):
  adapters:
    file:
      enabled: true
      paths:
        - /var/log/app/app.log
        - /var/log/app/error.log
      format: auto      # auto | json | plain | logfmt
      service: myapp
      encoding: utf-8
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from harness.monitoring.base_adapter import BaseLogAdapter
from harness.monitoring.log_event import LogEvent, LogLevel

# Regex patterns for common plain-text log formats
_PLAIN_PATTERNS = [
    # [2024-01-15 10:23:45] ERROR message
    re.compile(
        r"\[?(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\]]*?)\]?\s+"
        r"(?P<level>DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|FATAL)\s+(?P<msg>.+)",
        re.IGNORECASE,
    ),
    # 2024-01-15T10:23:45Z level=error msg="..."
    re.compile(
        r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s]*)\s+"
        r"level=(?P<level>\w+)\s+msg=\"?(?P<msg>[^\"]+)\"?",
        re.IGNORECASE,
    ),
    # Jan 15 10:23:45 host app[pid]: ERROR: message (syslog)
    re.compile(
        r"(?P<ts>\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+\S+\s+\S+:\s+"
        r"(?P<level>DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|FATAL):\s*(?P<msg>.+)",
        re.IGNORECASE,
    ),
]

_LOGFMT_PATTERN = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


class FileAdapter(BaseLogAdapter):
    """Reads log events from local files."""

    SOURCE_NAME = "file"

    def __init__(self, config: dict):
        super().__init__(config)
        self.paths = [Path(p) for p in config.get("paths", [])]
        self.fmt = config.get("format", "auto")
        self.service = config.get("service", "")
        self.encoding = config.get("encoding", "utf-8")

    def fetch(
        self,
        since: datetime,
        until: datetime,
        max_events: int = 500,
    ) -> list[LogEvent]:
        events = []
        for path in self.paths:
            if not path.exists():
                continue
            try:
                text = path.read_text(encoding=self.encoding, errors="replace")
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    event = self._parse_line(line)
                    if since <= event.timestamp <= until:
                        events.append(event)
                    if len(events) >= max_events:
                        break
            except Exception:
                continue
        return sorted(events, key=lambda e: e.timestamp)

    def stream(self) -> Iterator[LogEvent]:
        """Tail all configured log files indefinitely."""
        import time
        positions = {p: p.stat().st_size if p.exists() else 0 for p in self.paths}
        while True:
            for path in self.paths:
                if not path.exists():
                    continue
                try:
                    size = path.stat().st_size
                    if size > positions[path]:
                        with path.open(encoding=self.encoding, errors="replace") as f:
                            f.seek(positions[path])
                            for line in f:
                                line = line.strip()
                                if line:
                                    yield self._parse_line(line)
                        positions[path] = size
                except Exception:
                    continue
            time.sleep(0.5)

    def _parse_line(self, line: str) -> LogEvent:
        fmt = self.fmt
        if fmt == "auto":
            fmt = self._detect_format(line)

        if fmt == "json":
            return self._parse_json(line)
        if fmt == "logfmt":
            return self._parse_logfmt(line)
        return self._parse_plain(line)

    def _detect_format(self, line: str) -> str:
        stripped = line.lstrip()
        if stripped.startswith("{"):
            return "json"
        if "=" in line and not line.startswith("["):
            return "logfmt"
        return "plain"

    def _parse_json(self, line: str) -> LogEvent:
        try:
            d = json.loads(line)
            ts_str = d.get("timestamp") or d.get("ts") or d.get("time") or d.get("@timestamp", "")
            ts = _parse_ts(ts_str) or datetime.now(timezone.utc)
            level = LogLevel.from_string(
                d.get("level") or d.get("severity") or d.get("lvl") or "UNKNOWN"
            )
            message = (
                d.get("message") or d.get("msg") or d.get("text") or str(d)
            )
            return LogEvent(
                timestamp=ts, level=level, message=message,
                source=self.SOURCE_NAME, service=self.service,
                host=d.get("host", ""), trace_id=d.get("trace_id", ""),
                labels={k: str(v) for k, v in d.items()
                        if k not in ("timestamp", "ts", "time", "level", "message", "msg")},
                raw=line,
            )
        except Exception:
            return _fallback_event(line, self.SOURCE_NAME, self.service)

    def _parse_logfmt(self, line: str) -> LogEvent:
        try:
            pairs = {k: v.strip('"') for k, v in _LOGFMT_PATTERN.findall(line)}
            ts_str = pairs.get("ts") or pairs.get("time") or pairs.get("timestamp", "")
            ts = _parse_ts(ts_str) or datetime.now(timezone.utc)
            level = LogLevel.from_string(pairs.get("level") or pairs.get("lvl") or "UNKNOWN")
            message = pairs.get("msg") or pairs.get("message") or line
            return LogEvent(
                timestamp=ts, level=level, message=message,
                source=self.SOURCE_NAME, service=self.service,
                host=pairs.get("host", ""), raw=line, labels=pairs,
            )
        except Exception:
            return _fallback_event(line, self.SOURCE_NAME, self.service)

    def _parse_plain(self, line: str) -> LogEvent:
        for pattern in _PLAIN_PATTERNS:
            m = pattern.match(line)
            if m:
                ts = _parse_ts(m.group("ts")) or datetime.now(timezone.utc)
                level = LogLevel.from_string(m.group("level"))
                return LogEvent(
                    timestamp=ts, level=level, message=m.group("msg").strip(),
                    source=self.SOURCE_NAME, service=self.service, raw=line,
                )
        return _fallback_event(line, self.SOURCE_NAME, self.service)


class StdoutAdapter(BaseLogAdapter):
    """
    Reads log events from a subprocess's stdout/stderr.

    Config:
      adapters:
        stdout:
          enabled: true
          command: ["python", "-m", "myapp"]
          format: auto
          service: myapp
    """

    SOURCE_NAME = "stdout"

    def __init__(self, config: dict):
        super().__init__(config)
        self.command = config.get("command", [])
        self.fmt = config.get("format", "auto")
        self.service = config.get("service", "")
        self._file_adapter = FileAdapter({
            "format": self.fmt,
            "service": self.service,
        })

    def fetch(self, since: datetime, until: datetime, max_events: int = 500) -> list[LogEvent]:
        """Run the command, collect its output, parse as log events."""
        if not self.command:
            return []
        try:
            result = subprocess.run(
                self.command,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout + result.stderr
            events = []
            for line in output.splitlines():
                line = line.strip()
                if line:
                    event = self._file_adapter._parse_line(line)
                    if since <= event.timestamp <= until:
                        events.append(event)
            return sorted(events, key=lambda e: e.timestamp)[:max_events]
        except Exception:
            return []

    def stream(self) -> Iterator[LogEvent]:
        """Stream stdout/stderr from a long-running subprocess."""
        if not self.command:
            return
        proc = subprocess.Popen(
            self.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        for line in proc.stdout:
            line = line.strip()
            if line:
                yield self._file_adapter._parse_line(line)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    formats = [
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%b %d %H:%M:%S",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(s.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _fallback_event(line: str, source: str, service: str) -> LogEvent:
    """Return a best-effort event when parsing fails."""
    level = LogLevel.UNKNOWN
    for lvl in ("CRITICAL", "ERROR", "WARNING", "WARN", "INFO", "DEBUG"):
        if lvl in line.upper():
            level = LogLevel.from_string(lvl)
            break
    return LogEvent(
        timestamp=datetime.now(timezone.utc),
        level=level,
        message=line,
        source=source,
        service=service,
        raw=line,
    )
