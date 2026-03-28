"""
ingestor.py — LogIngestor

Source-agnostic log normalisation and windowing layer.
Sits between adapters and the LogMonitorAgent.

Responsibilities:
  - Polls all enabled adapters on a configurable interval
  - Builds LogWindows from the collected events
  - Triggers immediate analysis when error_rate exceeds spike_threshold
  - Deduplicates events across adapters (by timestamp + message hash)
  - Calls the provided on_window callback with each ready LogWindow
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from harness.monitoring.log_event import LogEvent, LogLevel, LogWindow


OnWindowCallback = Callable[[LogWindow], None]


class LogIngestor:
    """
    Polls all configured adapters and assembles LogWindows.

    Usage (polling mode):
        ingestor = LogIngestor(adapters, config)
        ingestor.run_forever(on_window=agent.analyse)

    Usage (one-shot trigger):
        window = ingestor.fetch_now()
        agent.analyse(window)
    """

    def __init__(self, adapters: list, config: dict):
        """
        adapters: list of BaseLogAdapter instances
        config:   monitoring_config.yaml["ingestor"] section
        """
        self.adapters = adapters
        self.poll_interval = config.get("poll_interval_seconds", 60)
        self.window_duration = config.get("window_duration_seconds", 300)
        self.max_events_per_window = config.get("max_events_per_window", 1000)
        self.spike_threshold = config.get("spike_error_rate_threshold", 0.10)
        self.min_events_for_spike = config.get("min_events_for_spike_detection", 10)
        self.dedup_enabled = config.get("dedup_enabled", True)
        self._seen_hashes: set[str] = set()
        self._seen_hashes_limit = 100_000   # prevent unbounded memory growth

    def fetch_now(self, duration_seconds: int = None) -> LogWindow:
        """
        One-shot fetch: poll all adapters and return a single merged LogWindow.
        Used for triggered (on-demand) analysis.
        """
        duration = duration_seconds or self.window_duration
        until = datetime.now(timezone.utc)
        since = until - timedelta(seconds=duration)

        all_events: list[LogEvent] = []
        for adapter in self.adapters:
            if not adapter.enabled:
                continue
            try:
                events = adapter.fetch(
                    since=since,
                    until=until,
                    max_events=self.max_events_per_window,
                )
                all_events.extend(events)
            except Exception as exc:
                print(f"  ⚠ Adapter {adapter.SOURCE_NAME} fetch failed: {exc}")

        deduped = self._dedup(all_events)
        sorted_events = sorted(deduped, key=lambda e: e.timestamp)
        source_names = ", ".join(
            {a.SOURCE_NAME for a in self.adapters if a.enabled}
        ) or "none"

        return LogWindow(
            events=sorted_events[:self.max_events_per_window],
            window_start=since,
            window_end=until,
            source=source_names,
        )

    def run_forever(self, on_window: OnWindowCallback) -> None:
        """
        Polling mode: fetch windows on a schedule, call on_window for each.
        Also calls on_window immediately if error rate spikes above threshold.
        Runs until interrupted (KeyboardInterrupt / SIGTERM).
        """
        print(f"  LogIngestor polling every {self.poll_interval}s "
              f"(window={self.window_duration}s, "
              f"spike_threshold={self.spike_threshold:.0%})")

        last_poll = datetime.now(timezone.utc) - timedelta(seconds=self.poll_interval)

        while True:
            now = datetime.now(timezone.utc)
            elapsed = (now - last_poll).total_seconds()

            if elapsed >= self.poll_interval:
                window = self.fetch_now()
                last_poll = now

                if len(window.events) > 0:
                    # Check for spike — trigger immediate analysis
                    if (window.total_count >= self.min_events_for_spike and
                            window.error_rate >= self.spike_threshold):
                        print(f"  🔴 Error spike detected: {window.error_rate:.1%} "
                              f"({window.error_count}/{window.total_count} errors) "
                              f"— triggering immediate analysis")
                        on_window(window)
                    elif window.error_count > 0 or window.critical_count > 0:
                        on_window(window)
                    # else: no errors in this window, skip analysis

            time.sleep(min(5, self.poll_interval / 4))

    def run_once(self, on_window: OnWindowCallback) -> None:
        """Single poll cycle. Useful for scheduled triggers (cron, CLI)."""
        window = self.fetch_now()
        if window.events:
            on_window(window)
        else:
            print("  No log events in current window.")

    def health_check_all(self) -> dict[str, tuple[bool, str]]:
        """Check connectivity for all adapters."""
        results = {}
        for adapter in self.adapters:
            results[adapter.SOURCE_NAME] = adapter.health_check()
        return results

    def _dedup(self, events: list[LogEvent]) -> list[LogEvent]:
        """Remove duplicate events based on timestamp + message hash."""
        if not self.dedup_enabled:
            return events

        unique = []
        for event in events:
            key = hashlib.md5(
                f"{event.timestamp.isoformat()}{event.message}".encode()
            ).hexdigest()
            if key not in self._seen_hashes:
                self._seen_hashes.add(key)
                unique.append(event)

        # Prevent unbounded growth
        if len(self._seen_hashes) > self._seen_hashes_limit:
            self._seen_hashes = set(list(self._seen_hashes)[-self._seen_hashes_limit // 2:])

        return unique
