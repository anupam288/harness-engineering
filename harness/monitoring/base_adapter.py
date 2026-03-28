"""
base_adapter.py — Abstract interface for all log source adapters.

Adding a new source (Splunk, CloudWatch, Elastic, etc.) means:
1. Create harness/monitoring/adapters/my_adapter.py
2. Subclass BaseLogAdapter
3. Implement fetch() and optionally stream()
4. Register it in monitoring_config.yaml

Nothing else in the pipeline changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Iterator

from harness.monitoring.log_event import LogEvent, LogWindow


class BaseLogAdapter(ABC):
    """
    Abstract base for all log source adapters.

    Subclasses implement:
      fetch(since, until, max_events) → list[LogEvent]
      stream()                         → Iterator[LogEvent]  (optional)

    The base class provides:
      fetch_window(duration_seconds)   → LogWindow
    """

    SOURCE_NAME: str = "unknown"   # override in subclass

    def __init__(self, config: dict):
        """
        config: the adapter's section from monitoring_config.yaml.
        Subclasses extract their own keys.
        """
        self.config = config
        self.enabled = config.get("enabled", True)

    @abstractmethod
    def fetch(
        self,
        since: datetime,
        until: datetime,
        max_events: int = 500,
    ) -> list[LogEvent]:
        """
        Fetch log events in the [since, until) time range.
        Must return events sorted oldest-first.
        Must never raise — return [] on any error and log the failure.
        """
        ...

    def stream(self) -> Iterator[LogEvent]:
        """
        Optional: yield LogEvents in real time (for file tail, stdout pipe).
        Default implementation raises NotImplementedError.
        Override in adapters that support live streaming.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support streaming. "
            "Use fetch() for polling-based adapters."
        )

    def fetch_window(self, duration_seconds: int = 300, max_events: int = 500) -> LogWindow:
        """
        Convenience: fetch a window of the last `duration_seconds` seconds.
        Returns a LogWindow ready for the LogMonitorAgent.
        """
        from datetime import timedelta
        until = datetime.now(timezone.utc)
        since = until - timedelta(seconds=duration_seconds)
        events = self.fetch(since=since, until=until, max_events=max_events)
        return LogWindow(
            events=events,
            window_start=since,
            window_end=until,
            source=self.SOURCE_NAME,
        )

    def health_check(self) -> tuple[bool, str]:
        """
        Check connectivity to the log source.
        Returns (is_healthy, message).
        Default: try to fetch 1 event from the last minute.
        """
        from datetime import timedelta
        try:
            until = datetime.now(timezone.utc)
            since = until - timedelta(minutes=1)
            self.fetch(since=since, until=until, max_events=1)
            return True, f"{self.SOURCE_NAME} reachable"
        except Exception as exc:
            return False, f"{self.SOURCE_NAME} unreachable: {exc}"
