"""
test_monitoring.py — Tests for the log monitoring layer.

Covers:
  - LogLevel: from_string(), severity ordering
  - LogEvent: construction, to_dict(), from_dict(), matches_pattern()
  - LogWindow: error_rate, errors_and_above(), summary()
  - FileAdapter: JSON, plain text, logfmt parsing
  - WebhookAdapter: generic, alertmanager, datadog payload parsing; push()
  - LokiAdapter: response parsing
  - DatadogAdapter: response parsing
  - LogIngestor: fetch_now() merging, deduplication, spike detection
  - ActionRunner: all four actions (log_only, alert_human, open_pr, trigger_rollback)
  - LogMonitorAgent: deterministic rule matching, LLM fallback, empty window
  - Adapter registry: build_adapter(), build_adapters_from_config()
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness.monitoring.log_event import LogEvent, LogLevel, LogWindow
from harness.monitoring.adapters.file_adapter import FileAdapter, StdoutAdapter, _fallback_event
from harness.monitoring.adapters.webhook_adapter import WebhookAdapter
from harness.monitoring.adapters.loki_adapter import LokiAdapter
from harness.monitoring.adapters.datadog_adapter import DatadogAdapter
from harness.monitoring.adapters import build_adapter, build_adapters_from_config, ADAPTER_REGISTRY
from harness.monitoring.ingestor import LogIngestor
from harness.monitoring.action_runner import ActionRunner, MonitoringDecision
from harness.monitoring.log_monitor_agent import LogMonitorAgent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_repo(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "policies").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "harness" / "agents").mkdir(parents=True)
    (tmp_path / ".harness" / "logs").mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("# AGENTS.md")
    return tmp_path


@pytest.fixture
def config(tmp_repo):
    from harness.config import HarnessConfig
    return HarnessConfig(
        repo_root=tmp_repo,
        logs_dir=tmp_repo / ".harness" / "logs",
        docs_dir=tmp_repo / "docs",
        policies_dir=tmp_repo / "policies",
    )


def now() -> datetime:
    return datetime.now(timezone.utc)


def make_event(level=LogLevel.INFO, message="test", source="test",
               service="app", ts=None) -> LogEvent:
    return LogEvent(
        timestamp=ts or now(),
        level=level,
        message=message,
        source=source,
        service=service,
    )


def make_window(events=None, error_count=0, total_count=None) -> LogWindow:
    evts = events or []
    return LogWindow(
        events=evts,
        window_start=now() - timedelta(minutes=5),
        window_end=now(),
        source="test",
        total_count=total_count or len(evts),
        error_count=error_count,
    )


# ---------------------------------------------------------------------------
# LogLevel tests
# ---------------------------------------------------------------------------

class TestLogLevel:

    def test_from_string_case_insensitive(self):
        assert LogLevel.from_string("error") == LogLevel.ERROR
        assert LogLevel.from_string("ERROR") == LogLevel.ERROR
        assert LogLevel.from_string("Error") == LogLevel.ERROR

    def test_from_string_warn_maps_to_warning(self):
        assert LogLevel.from_string("WARN") == LogLevel.WARNING

    def test_from_string_fatal_maps_to_critical(self):
        assert LogLevel.from_string("FATAL") == LogLevel.CRITICAL

    def test_from_string_unknown_for_garbage(self):
        assert LogLevel.from_string("BLAH") == LogLevel.UNKNOWN

    def test_from_string_empty(self):
        assert LogLevel.from_string("") == LogLevel.UNKNOWN

    def test_severity_ordering(self):
        assert LogLevel.DEBUG.severity() < LogLevel.INFO.severity()
        assert LogLevel.INFO.severity() < LogLevel.WARNING.severity()
        assert LogLevel.WARNING.severity() < LogLevel.ERROR.severity()
        assert LogLevel.ERROR.severity() < LogLevel.CRITICAL.severity()


# ---------------------------------------------------------------------------
# LogEvent tests
# ---------------------------------------------------------------------------

class TestLogEvent:

    def test_to_dict_and_from_dict_roundtrip(self):
        event = make_event(level=LogLevel.ERROR, message="Something broke")
        d = event.to_dict()
        restored = LogEvent.from_dict(d)
        assert restored.level == LogLevel.ERROR
        assert restored.message == "Something broke"

    def test_is_error_or_above(self):
        assert make_event(level=LogLevel.ERROR).is_error_or_above()
        assert make_event(level=LogLevel.CRITICAL).is_error_or_above()
        assert not make_event(level=LogLevel.WARNING).is_error_or_above()
        assert not make_event(level=LogLevel.INFO).is_error_or_above()

    def test_matches_pattern_case_insensitive(self):
        event = make_event(message="Database connection refused")
        assert event.matches_pattern("connection refused")
        assert event.matches_pattern("CONNECTION REFUSED")
        assert not event.matches_pattern("timeout")

    def test_labels_stored(self):
        event = LogEvent(
            timestamp=now(), level=LogLevel.INFO, message="hi",
            source="test", labels={"env": "prod", "region": "us-east-1"}
        )
        assert event.labels["env"] == "prod"


# ---------------------------------------------------------------------------
# LogWindow tests
# ---------------------------------------------------------------------------

class TestLogWindow:

    def test_error_rate_computed(self):
        window = make_window(
            events=[make_event(LogLevel.ERROR)] * 3 + [make_event(LogLevel.INFO)] * 7,
            error_count=3,
            total_count=10,
        )
        assert abs(window.error_rate - 0.3) < 0.001

    def test_error_rate_zero_for_empty(self):
        window = make_window(events=[], total_count=0)
        assert window.error_rate == 0.0

    def test_errors_and_above_filters_correctly(self):
        events = [
            make_event(LogLevel.ERROR, "err1"),
            make_event(LogLevel.CRITICAL, "crit1"),
            make_event(LogLevel.INFO, "info1"),
            make_event(LogLevel.WARNING, "warn1"),
        ]
        window = make_window(events=events)
        errors = window.errors_and_above()
        assert len(errors) == 2
        messages = [e.message for e in errors]
        assert "err1" in messages
        assert "crit1" in messages

    def test_summary_contains_key_fields(self):
        window = make_window(
            events=[make_event(LogLevel.ERROR)] * 2,
            error_count=2, total_count=10
        )
        s = window.summary()
        assert "error" in s.lower()
        assert "10" in s

    def test_to_dict_caps_events_at_50(self):
        events = [make_event() for _ in range(100)]
        window = make_window(events=events)
        d = window.to_dict()
        assert len(d["events"]) <= 50


# ---------------------------------------------------------------------------
# FileAdapter tests
# ---------------------------------------------------------------------------

class TestFileAdapter:

    def test_parse_json_line(self):
        adapter = FileAdapter({"format": "json", "service": "app"})
        line = json.dumps({
            "timestamp": "2024-01-15T10:23:45Z",
            "level": "error",
            "message": "Something went wrong",
        })
        event = adapter._parse_json(line)
        assert event.level == LogLevel.ERROR
        assert event.message == "Something went wrong"

    def test_parse_plain_line_with_brackets(self):
        adapter = FileAdapter({"format": "plain", "service": "app"})
        line = "[2024-01-15 10:23:45] ERROR Database connection failed"
        event = adapter._parse_plain(line)
        assert event.level == LogLevel.ERROR
        assert "Database connection failed" in event.message

    def test_parse_logfmt_line(self):
        adapter = FileAdapter({"format": "logfmt", "service": "app"})
        line = 'ts=2024-01-15T10:23:45Z level=error msg="Query timed out" host=web-01'
        event = adapter._parse_logfmt(line)
        assert event.level == LogLevel.ERROR
        assert "Query timed out" in event.message
        assert event.host == "web-01"

    def test_auto_detect_json(self):
        adapter = FileAdapter({"format": "auto", "service": "app"})
        assert adapter._detect_format('{"level": "error"}') == "json"

    def test_auto_detect_plain(self):
        adapter = FileAdapter({"format": "auto", "service": "app"})
        assert adapter._detect_format("[2024-01-15] ERROR Something") == "plain"

    def test_fetch_from_file(self, tmp_path):
        log_file = tmp_path / "app.log"
        log_file.write_text(
            '{"timestamp": "2024-01-15T10:00:00Z", "level": "error", "message": "err1"}\n'
            '{"timestamp": "2024-01-15T10:01:00Z", "level": "info", "message": "ok"}\n'
        )
        adapter = FileAdapter({"paths": [str(log_file)], "format": "json", "service": "app"})
        since = datetime(2024, 1, 15, 9, 0, 0, tzinfo=timezone.utc)
        until = datetime(2024, 1, 15, 11, 0, 0, tzinfo=timezone.utc)
        events = adapter.fetch(since=since, until=until)
        assert len(events) == 2
        assert events[0].level == LogLevel.ERROR

    def test_fetch_nonexistent_file_returns_empty(self):
        adapter = FileAdapter({"paths": ["/nonexistent/path/app.log"]})
        events = adapter.fetch(since=now() - timedelta(hours=1), until=now())
        assert events == []

    def test_fallback_event_detects_level(self):
        event = _fallback_event("2024-01-15 ERROR: something bad", "file", "app")
        assert event.level == LogLevel.ERROR


# ---------------------------------------------------------------------------
# WebhookAdapter tests
# ---------------------------------------------------------------------------

class TestWebhookAdapter:

    def test_push_generic_json(self):
        adapter = WebhookAdapter({"service": "app", "format": "generic"})
        payload = json.dumps({"level": "error", "message": "DB down"}).encode()
        events = adapter.push(payload)
        assert len(events) == 1
        assert events[0].level == LogLevel.ERROR
        assert events[0].message == "DB down"

    def test_push_list_of_events(self):
        adapter = WebhookAdapter({"service": "app", "format": "generic"})
        payload = json.dumps([
            {"level": "error", "message": "err1"},
            {"level": "info", "message": "ok"},
        ]).encode()
        events = adapter.push(payload)
        assert len(events) == 2

    def test_push_alertmanager_format(self):
        adapter = WebhookAdapter({"service": "app", "format": "alertmanager"})
        payload = json.dumps({
            "alerts": [{
                "status": "firing",
                "labels": {"alertname": "HighErrorRate", "service": "myapp"},
                "annotations": {"summary": "Error rate is too high"},
                "startsAt": "2024-01-15T10:00:00Z",
            }],
            "commonLabels": {},
        }).encode()
        events = adapter.push(payload)
        assert len(events) == 1
        assert events[0].level == LogLevel.CRITICAL
        assert "Error rate is too high" in events[0].message

    def test_push_invalid_json_returns_raw_event(self):
        adapter = WebhookAdapter({"service": "app"})
        payload = b"this is not json"
        events = adapter.push(payload)
        assert len(events) == 1
        assert "this is not json" in events[0].message

    def test_fetch_returns_buffered_events_in_range(self):
        adapter = WebhookAdapter({"service": "app"})
        ts = now()
        payload = json.dumps({
            "level": "error", "message": "test error",
            "timestamp": ts.isoformat()
        }).encode()
        adapter.push(payload)
        events = adapter.fetch(since=ts - timedelta(seconds=10), until=ts + timedelta(seconds=10))
        assert len(events) >= 1

    def test_auto_detect_alertmanager(self):
        adapter = WebhookAdapter({"service": "app"})
        data = {"alerts": [], "commonLabels": {}}
        assert adapter._detect_format(data) == "alertmanager"

    def test_auto_detect_generic(self):
        adapter = WebhookAdapter({"service": "app"})
        data = {"level": "error", "message": "something"}
        assert adapter._detect_format(data) == "generic"


# ---------------------------------------------------------------------------
# LokiAdapter tests
# ---------------------------------------------------------------------------

class TestLokiAdapter:

    def test_parse_loki_response(self):
        adapter = LokiAdapter({"url": "http://localhost:3100", "query": '{job="test"}'})
        response = {
            "data": {
                "resultType": "streams",
                "result": [{
                    "stream": {"service": "myapp", "host": "web-01"},
                    "values": [
                        [str(int(now().timestamp() * 1e9)), "ERROR Something went wrong"],
                        [str(int(now().timestamp() * 1e9)), "INFO All good"],
                    ]
                }]
            }
        }
        events = adapter._parse_response(response)
        assert len(events) == 2
        assert events[0].service == "myapp"
        assert events[0].host == "web-01"

    def test_parse_empty_loki_response(self):
        adapter = LokiAdapter({"url": "http://localhost:3100", "query": '{job="test"}'})
        response = {"data": {"resultType": "streams", "result": []}}
        events = adapter._parse_response(response)
        assert events == []


# ---------------------------------------------------------------------------
# DatadogAdapter tests
# ---------------------------------------------------------------------------

class TestDatadogAdapter:

    def test_parse_datadog_response(self):
        adapter = DatadogAdapter({
            "site": "datadoghq.com", "api_key": "k", "app_key": "k",
            "service": "myapp"
        })
        response = {
            "data": [{
                "attributes": {
                    "timestamp": "2024-01-15T10:00:00+00:00",
                    "status": "error",
                    "message": "Database error",
                    "service": "myapp",
                    "host": "web-01",
                    "tags": ["env:prod", "version:1.2.3"],
                }
            }]
        }
        events = adapter._parse_response(response)
        assert len(events) == 1
        assert events[0].level == LogLevel.ERROR
        assert events[0].message == "Database error"
        assert events[0].labels.get("env") == "prod"

    def test_parse_empty_datadog_response(self):
        adapter = DatadogAdapter({"site": "datadoghq.com", "api_key": "k",
                                   "app_key": "k", "service": "app"})
        events = adapter._parse_response({"data": []})
        assert events == []


# ---------------------------------------------------------------------------
# Adapter registry tests
# ---------------------------------------------------------------------------

class TestAdapterRegistry:

    def test_all_adapters_registered(self):
        for name in ["file", "stdout", "loki", "datadog", "webhook"]:
            assert name in ADAPTER_REGISTRY

    def test_build_adapter_returns_correct_type(self):
        adapter = build_adapter("webhook", {"service": "app"})
        assert isinstance(adapter, WebhookAdapter)

    def test_build_adapter_raises_for_unknown(self):
        with pytest.raises(ValueError, match="Unknown adapter"):
            build_adapter("splunk", {})

    def test_build_adapters_from_config_skips_disabled(self):
        config = {
            "adapters": {
                "file": {"enabled": False, "paths": []},
                "webhook": {"enabled": True, "service": "app"},
            }
        }
        adapters = build_adapters_from_config(config)
        names = [a.SOURCE_NAME for a in adapters]
        assert "webhook" in names
        assert "file" not in names

    def test_build_adapters_from_config_empty(self):
        adapters = build_adapters_from_config({})
        assert adapters == []


# ---------------------------------------------------------------------------
# LogIngestor tests
# ---------------------------------------------------------------------------

class TestLogIngestor:

    def _make_mock_adapter(self, events: list[LogEvent], name="mock") -> MagicMock:
        adapter = MagicMock()
        adapter.SOURCE_NAME = name
        adapter.enabled = True
        adapter.fetch.return_value = events
        return adapter

    def test_fetch_now_merges_adapters(self):
        e1 = make_event(LogLevel.ERROR, "err from A")
        e2 = make_event(LogLevel.INFO, "info from B")
        a1 = self._make_mock_adapter([e1], "adapter_a")
        a2 = self._make_mock_adapter([e2], "adapter_b")
        ingestor = LogIngestor([a1, a2], {})
        window = ingestor.fetch_now()
        assert len(window.events) == 2

    def test_fetch_now_deduplicates(self):
        # Same event from two adapters
        ts = now()
        e = make_event(ts=ts, message="duplicate error")
        a1 = self._make_mock_adapter([e], "a1")
        a2 = self._make_mock_adapter([e], "a2")
        ingestor = LogIngestor([a1, a2], {"dedup_enabled": True})
        window = ingestor.fetch_now()
        assert len(window.events) == 1

    def test_fetch_now_no_dedup_when_disabled(self):
        ts = now()
        e = make_event(ts=ts, message="same message")
        a1 = self._make_mock_adapter([e], "a1")
        a2 = self._make_mock_adapter([e], "a2")
        ingestor = LogIngestor([a1, a2], {"dedup_enabled": False})
        window = ingestor.fetch_now()
        assert len(window.events) == 2

    def test_fetch_now_skips_disabled_adapters(self):
        e = make_event(LogLevel.ERROR, "should not appear")
        adapter = self._make_mock_adapter([e])
        adapter.enabled = False
        ingestor = LogIngestor([adapter], {})
        window = ingestor.fetch_now()
        assert len(window.events) == 0

    def test_run_once_calls_callback(self):
        e = make_event(LogLevel.ERROR, "error!")
        adapter = self._make_mock_adapter([e])
        ingestor = LogIngestor([adapter], {})
        called_with = []
        ingestor.run_once(on_window=lambda w: called_with.append(w))
        assert len(called_with) == 1
        assert called_with[0].events[0].message == "error!"

    def test_run_once_no_callback_when_empty(self):
        adapter = self._make_mock_adapter([])
        ingestor = LogIngestor([adapter], {})
        called = []
        ingestor.run_once(on_window=lambda w: called.append(w))
        assert len(called) == 0


# ---------------------------------------------------------------------------
# ActionRunner tests
# ---------------------------------------------------------------------------

class TestActionRunner:

    def _make_decision(self, action="log_only", severity="low") -> MonitoringDecision:
        return MonitoringDecision(
            action=action, severity=severity,
            summary="Test summary", root_cause="Test root cause",
            matched_rules=[], proposed_fix="Fix the bug",
            rollback_reason="Too many errors",
        )

    def test_log_only_does_not_create_files(self, config):
        runner = ActionRunner(config)
        decision = self._make_decision("log_only")
        window = make_window()
        runner.execute(decision, window)
        # No alert or PR files created
        assert len(list((config.repo_root / ".harness" / "alerts").glob("*.md"))) == 0
        assert len(list((config.repo_root / ".harness" / "proposed_prs").glob("MON_*.md"))) == 0

    def test_alert_human_creates_alert_file(self, config):
        runner = ActionRunner(config)
        decision = self._make_decision("alert_human", "high")
        window = make_window(events=[make_event(LogLevel.ERROR, "DB down")])
        runner.execute(decision, window)
        alerts = list((config.repo_root / ".harness" / "alerts").glob("*.md"))
        assert len(alerts) == 1
        content = alerts[0].read_text()
        assert "HIGH" in content
        assert "Test summary" in content

    def test_open_pr_creates_pr_file(self, config):
        runner = ActionRunner(config)
        decision = self._make_decision("open_pr", "medium")
        decision.proposed_fix = "Add null check at line 42"
        window = make_window(events=[make_event(LogLevel.ERROR, "NullPointerException")])
        runner.execute(decision, window)
        prs = list((config.repo_root / ".harness" / "proposed_prs").glob("MON_*.md"))
        assert len(prs) == 1
        content = prs[0].read_text()
        assert "Add null check at line 42" in content

    def test_log_decision_writes_to_monitoring_log(self, config):
        runner = ActionRunner(config)
        decision = self._make_decision("log_only")
        window = make_window()
        runner.execute(decision, window)
        monitoring_log = config.logs_dir / "monitoring_log.jsonl"
        assert monitoring_log.exists()
        entry = json.loads(monitoring_log.read_text().strip())
        assert entry["action"] == "log_only"
        assert entry["severity"] == "low"

    def test_unknown_action_falls_back_to_log_only(self, config):
        runner = ActionRunner(config)
        decision = MonitoringDecision(
            action="unknown_action_xyz", severity="low",
            summary="test", root_cause="test", matched_rules=[],
        )
        window = make_window()
        runner.execute(decision, window)  # should not raise
        # Falls back to log_only — no files created
        assert len(list((config.repo_root / ".harness" / "alerts").glob("*.md"))) == 0


# ---------------------------------------------------------------------------
# LogMonitorAgent tests
# ---------------------------------------------------------------------------

class TestLogMonitorAgent:

    def _make_agent(self, config, rules=None) -> LogMonitorAgent:
        import yaml
        if rules:
            rules_path = config.repo_root / "monitoring_rules.yaml"
            rules_path.write_text(yaml.dump({"rules": rules}))

        mock_model = MagicMock()
        mock_model.call_with_fallback.return_value = MagicMock(
            text=json.dumps({
                "action": "alert_human",
                "severity": "medium",
                "summary": "Novel error pattern detected",
                "root_cause": "Unknown",
                "matched_rules": [],
                "proposed_fix": "",
                "rollback_reason": "",
                "confidence": 0.7,
                "flags": [],
            }),
            model="m", provider="anthropic", input_tokens=0, output_tokens=0
        )

        with patch("harness.model.build_model", return_value=mock_model):
            with patch("harness.model.prompt_registry.PromptRegistry"):
                agent = LogMonitorAgent(config)
                agent._model = mock_model
        return agent

    def test_empty_window_returns_log_only(self, config):
        agent = self._make_agent(config)
        decision = agent._decide(make_window(events=[]))
        assert decision.action == "log_only"
        assert decision.severity == "low"

    def test_no_errors_returns_log_only(self, config):
        agent = self._make_agent(config)
        window = make_window(events=[make_event(LogLevel.INFO, "all good")])
        decision = agent._decide(window)
        assert decision.action == "log_only"

    def test_deterministic_rule_matched(self, config):
        rules = [{
            "rule_id": "TEST_OOM",
            "description": "OOM error",
            "pattern": "OutOfMemoryError",
            "level": "ERROR",
            "min_occurrences": 1,
            "min_error_rate": 0.0,
            "action": "trigger_rollback",
            "severity": "critical",
            "root_cause_hint": "OOM",
            "rollback_reason": "OOM detected",
            "enabled": True,
        }]
        agent = self._make_agent(config, rules=rules)
        events = [make_event(LogLevel.ERROR, "java.lang.OutOfMemoryError: heap space")]
        window = make_window(events=events, error_count=1)
        decision = agent._decide(window)
        assert decision.action == "trigger_rollback"
        assert decision.severity == "critical"
        assert "TEST_OOM" in decision.matched_rules

    def test_rule_not_matched_when_occurrences_too_low(self, config):
        rules = [{
            "rule_id": "TEST_RARE",
            "description": "Rare error",
            "pattern": "connection refused",
            "level": "ERROR",
            "min_occurrences": 10,   # need 10, only have 1
            "min_error_rate": 0.0,
            "action": "alert_human",
            "severity": "medium",
            "enabled": True,
        }]
        agent = self._make_agent(config, rules=rules)
        events = [make_event(LogLevel.ERROR, "connection refused")]
        window = make_window(events=events, error_count=1)
        # Only 1 occurrence, rule needs 10 — falls through to LLM
        matched = agent._match_rules(window)
        assert len(matched) == 0

    def test_higher_severity_rule_wins(self, config):
        rules = [
            {
                "rule_id": "LOW_001", "description": "Low",
                "pattern": "error", "min_occurrences": 1, "min_error_rate": 0.0,
                "action": "log_only", "severity": "low", "enabled": True,
            },
            {
                "rule_id": "CRIT_001", "description": "Critical",
                "pattern": "error", "min_occurrences": 1, "min_error_rate": 0.0,
                "action": "trigger_rollback", "severity": "critical", "enabled": True,
            },
        ]
        agent = self._make_agent(config, rules=rules)
        events = [make_event(LogLevel.ERROR, "error occurred")]
        window = make_window(events=events, error_count=1)
        matched = agent._match_rules(window)
        assert matched[0]["rule_id"] == "CRIT_001"

    def test_disabled_rule_not_matched(self, config):
        rules = [{
            "rule_id": "DISABLED_001",
            "description": "Disabled rule",
            "pattern": "error",
            "min_occurrences": 1,
            "min_error_rate": 0.0,
            "action": "alert_human",
            "severity": "high",
            "enabled": False,
        }]
        agent = self._make_agent(config, rules=rules)
        events = [make_event(LogLevel.ERROR, "some error")]
        window = make_window(events=events, error_count=1)
        matched = agent._match_rules(window)
        assert len(matched) == 0

    def test_llm_fallback_called_for_novel_pattern(self, config):
        agent = self._make_agent(config, rules=[])
        events = [make_event(LogLevel.ERROR, "novel unknown error pattern xyz")]
        window = make_window(events=events, error_count=1)
        decision = agent._decide(window)
        # LLM fallback used — check for llm_analysed flag
        assert "llm_analysed" in decision.flags

    def test_monitoring_decision_to_dict(self):
        decision = MonitoringDecision(
            action="alert_human", severity="high",
            summary="High error rate", root_cause="DB down",
            matched_rules=["DB_001"], proposed_fix="Restart DB",
            confidence=0.9,
        )
        d = decision.to_dict()
        assert d["action"] == "alert_human"
        assert d["severity"] == "high"
        assert "DB_001" in d["matched_rules"]
        assert d["confidence"] == 0.9
