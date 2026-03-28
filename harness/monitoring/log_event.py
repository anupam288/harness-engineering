"""
log_event.py — Core data structures for the log monitoring layer.

LogEvent is the normalised representation of a single log line,
regardless of which adapter produced it. All adapters must emit LogEvents.
The rest of the pipeline (ingestor, agent, action runner) is source-agnostic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_string(cls, s: str) -> "LogLevel":
        """Parse a log level string from any format."""
        if not s:
            return cls.UNKNOWN
        normalised = s.upper().strip()
        mapping = {
            "DEBUG": cls.DEBUG,
            "INFO": cls.INFO,
            "WARN": cls.WARNING,
            "WARNING": cls.WARNING,
            "ERROR": cls.ERROR,
            "ERR": cls.ERROR,
            "CRITICAL": cls.CRITICAL,
            "CRIT": cls.CRITICAL,
            "FATAL": cls.CRITICAL,
        }
        return mapping.get(normalised, cls.UNKNOWN)

    def severity(self) -> int:
        """Numeric severity for comparison. Higher = more severe."""
        return {
            LogLevel.DEBUG: 1,
            LogLevel.INFO: 2,
            LogLevel.WARNING: 3,
            LogLevel.ERROR: 4,
            LogLevel.CRITICAL: 5,
            LogLevel.UNKNOWN: 0,
        }[self]


@dataclass
class LogEvent:
    """
    Normalised log event — the common currency of the monitoring pipeline.

    Every adapter maps its native format to this structure.
    Downstream components (ingestor, agent, action runner) only see LogEvents.
    """

    timestamp: datetime
    level: LogLevel
    message: str
    source: str                        # adapter name: "loki", "datadog", "file", etc.
    service: str = ""                  # application service name if available
    trace_id: str = ""                 # distributed trace ID if available
    span_id: str = ""                  # span ID if available
    host: str = ""                     # hostname / pod name
    labels: dict[str, str] = field(default_factory=dict)   # arbitrary key-value tags
    raw: str = ""                      # original raw log line (for debugging)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "level": self.level.value,
            "message": self.message,
            "source": self.source,
            "service": self.service,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "host": self.host,
            "labels": self.labels,
        }

    def is_error_or_above(self) -> bool:
        return self.level.severity() >= LogLevel.ERROR.severity()

    def matches_pattern(self, pattern: str) -> bool:
        """Case-insensitive substring match against message."""
        return pattern.lower() in self.message.lower()

    @classmethod
    def from_dict(cls, d: dict) -> "LogEvent":
        return cls(
            timestamp=datetime.fromisoformat(d.get("timestamp",
                datetime.now(timezone.utc).isoformat())),
            level=LogLevel.from_string(d.get("level", "UNKNOWN")),
            message=d.get("message", ""),
            source=d.get("source", "unknown"),
            service=d.get("service", ""),
            trace_id=d.get("trace_id", ""),
            span_id=d.get("span_id", ""),
            host=d.get("host", ""),
            labels=d.get("labels", {}),
            raw=d.get("raw", ""),
        )


@dataclass
class LogWindow:
    """
    A time-bounded slice of LogEvents passed to the LogMonitorAgent.

    The ingestor fills windows and hands them to the agent
    either on a schedule (polling) or when error rate spikes.
    """

    events: list[LogEvent]
    window_start: datetime
    window_end: datetime
    source: str
    total_count: int = 0       # total events in window (may exceed len(events) if sampled)
    error_count: int = 0
    warning_count: int = 0
    critical_count: int = 0

    def __post_init__(self):
        self.total_count = self.total_count or len(self.events)
        self.error_count = self.error_count or sum(
            1 for e in self.events if e.level == LogLevel.ERROR
        )
        self.warning_count = self.warning_count or sum(
            1 for e in self.events if e.level == LogLevel.WARNING
        )
        self.critical_count = self.critical_count or sum(
            1 for e in self.events if e.level == LogLevel.CRITICAL
        )

    @property
    def error_rate(self) -> float:
        if self.total_count == 0:
            return 0.0
        return (self.error_count + self.critical_count) / self.total_count

    @property
    def duration_seconds(self) -> float:
        return (self.window_end - self.window_start).total_seconds()

    def errors_and_above(self) -> list[LogEvent]:
        return [e for e in self.events if e.is_error_or_above()]

    def summary(self) -> str:
        return (
            f"Window [{self.window_start.strftime('%H:%M:%S')} – "
            f"{self.window_end.strftime('%H:%M:%S')}] "
            f"source={self.source} "
            f"total={self.total_count} "
            f"errors={self.error_count} "
            f"critical={self.critical_count} "
            f"error_rate={self.error_rate:.1%}"
        )

    def to_dict(self) -> dict:
        return {
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "source": self.source,
            "total_count": self.total_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "critical_count": self.critical_count,
            "error_rate": self.error_rate,
            "events": [e.to_dict() for e in self.events[:50]],  # cap at 50 for context
        }
