"""
test_observability.py — Tests for the observability stack.

Covers:
  - MetricsCollector: record(), cost estimation, budget warnings, read_all()
  - MetricsAggregator: summarise(), per_agent(), cost_by_phase(),
    confidence_over_time(), percentile computation, trend detection
  - BudgetMonitor: check_run(), check_summary(), alert levels
  - HarnessConfig.observability_config() and metrics_collector()
  - BaseAgent.execute() automatically records metrics
  - CLI metrics command (smoke test)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness.agents.base_agent import AgentResult
from harness.observability.aggregator import (
    MetricsAggregator,
    _compute_trend,
    _percentile,
)
from harness.observability.budget import BudgetAlert, BudgetMonitor
from harness.observability.metrics import MetricsCollector, MetricsEntry


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
def logs_dir(tmp_repo):
    return tmp_repo / ".harness" / "logs"


@pytest.fixture
def config(tmp_repo):
    from harness.config import HarnessConfig
    return HarnessConfig(
        repo_root=tmp_repo,
        logs_dir=tmp_repo / ".harness" / "logs",
        docs_dir=tmp_repo / "docs",
        policies_dir=tmp_repo / "policies",
    )


def make_result(
    agent_name="TestAgent",
    phase="development",
    status="pass",
    confidence=0.85,
    flags=None,
) -> AgentResult:
    return AgentResult(
        agent_name=agent_name,
        phase=phase,
        status=status,
        output={"result": "ok"},
        confidence=confidence,
        flags=flags or [],
    )


def seed_metrics(collector: MetricsCollector, entries: list[dict]) -> None:
    """Write raw metric entries directly to metrics_log.jsonl for test setup."""
    for e in entries:
        with collector.metrics_path.open("a") as f:
            f.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# MetricsCollector tests
# ---------------------------------------------------------------------------

class TestMetricsCollector:

    def test_record_creates_metrics_log(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        result = make_result()
        collector.record(result, model_id="claude-sonnet-4-20250514",
                         input_tokens=100, output_tokens=50, latency_seconds=1.2)
        assert collector.metrics_path.exists()

    def test_record_appends_jsonl_entry(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        collector.record(make_result(), input_tokens=100, output_tokens=50)
        collector.record(make_result(agent_name="OtherAgent"), input_tokens=200)
        entries = collector.read_all()
        assert len(entries) == 2
        assert entries[0]["agent_name"] == "TestAgent"
        assert entries[1]["agent_name"] == "OtherAgent"

    def test_record_returns_metrics_entry(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        entry = collector.record(make_result(), input_tokens=500, output_tokens=250,
                                  latency_seconds=2.5, run_id="abc123")
        assert isinstance(entry, MetricsEntry)
        assert entry.total_tokens == 750
        assert entry.latency_seconds == 2.5
        assert entry.run_id == "abc123"

    def test_cost_estimation_anthropic_sonnet(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        # 1M input @ $3.00 + 0.5M output @ $15.00 = $3.00 + $7.50 = $10.50
        entry = collector.record(
            make_result(),
            model_id="claude-sonnet-4-20250514",
            input_tokens=1_000_000,
            output_tokens=500_000,
        )
        assert abs(entry.cost_usd - 10.50) < 0.001

    def test_cost_estimation_haiku_cheaper(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        entry_sonnet = collector.record(
            make_result(), model_id="claude-sonnet-4-20250514",
            input_tokens=10_000, output_tokens=5_000
        )
        entry_haiku = collector.record(
            make_result(), model_id="claude-haiku-4-5-20251001",
            input_tokens=10_000, output_tokens=5_000
        )
        assert entry_haiku.cost_usd < entry_sonnet.cost_usd

    def test_cost_uses_default_for_unknown_model(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        entry = collector.record(make_result(), model_id="unknown-model-xyz",
                                  input_tokens=1_000, output_tokens=500)
        assert entry.cost_usd > 0  # used default pricing, didn't crash

    def test_budget_warning_per_run_tokens(self, logs_dir, capsys):
        collector = MetricsCollector(logs_dir, budgets={"alert_per_run_tokens": 100})
        collector.record(make_result(), input_tokens=80, output_tokens=50)  # total 130 > 100
        captured = capsys.readouterr()
        assert "BUDGET ALERT" in captured.out
        assert "tokens" in captured.out.lower()

    def test_no_budget_warning_when_under_threshold(self, logs_dir, capsys):
        collector = MetricsCollector(logs_dir, budgets={"alert_per_run_tokens": 10_000})
        collector.record(make_result(), input_tokens=50, output_tokens=50)
        captured = capsys.readouterr()
        assert "BUDGET ALERT" not in captured.out

    def test_budget_warning_per_run_cost(self, logs_dir, capsys):
        collector = MetricsCollector(
            logs_dir,
            budgets={"alert_per_run_cost_usd": 0.000001}  # very low threshold
        )
        collector.record(make_result(), model_id="claude-sonnet-4-20250514",
                          input_tokens=1000, output_tokens=1000)
        captured = capsys.readouterr()
        assert "BUDGET ALERT" in captured.out

    def test_review_iterations_captured(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        result = make_result()
        result.review_metadata = {"iterations": 3, "approved": True, "all_reviews": []}
        entry = collector.record(result)
        assert entry.review_iterations == 3
        saved = collector.read_all()
        assert saved[0]["review_iterations"] == 3

    def test_read_all_empty_when_no_log(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        assert collector.read_all() == []

    def test_custom_pricing_overrides_default(self, logs_dir):
        custom_pricing = {"my-model": {"input": 100.0, "output": 100.0}}
        collector = MetricsCollector(logs_dir, pricing=custom_pricing)
        entry = collector.record(make_result(), model_id="my-model",
                                  input_tokens=1_000_000, output_tokens=0)
        assert abs(entry.cost_usd - 100.0) < 0.001


# ---------------------------------------------------------------------------
# MetricsAggregator tests
# ---------------------------------------------------------------------------

class TestMetricsAggregator:

    def _make_entry(self, agent="A", phase="dev", status="pass",
                    confidence=0.9, latency=1.0, tokens=100, cost=0.01) -> dict:
        from datetime import datetime, timezone
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": "test",
            "agent_name": agent,
            "phase": phase,
            "model_id": "claude-sonnet-4-20250514",
            "provider": "anthropic",
            "input_tokens": tokens,
            "output_tokens": 0,
            "total_tokens": tokens,
            "latency_seconds": latency,
            "status": status,
            "confidence": confidence,
            "cost_usd": cost,
            "review_iterations": 0,
            "flags": [],
        }

    def test_empty_log_returns_zero_summary(self, logs_dir):
        agg = MetricsAggregator(logs_dir)
        summary = agg.summarise()
        assert summary.total_runs == 0
        assert summary.total_cost_usd == 0.0
        assert summary.harness_health_score == 0.0

    def test_summarise_counts_runs(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        entries = [self._make_entry() for _ in range(5)]
        for e in entries:
            seed_metrics(collector, [e])
        agg = MetricsAggregator(logs_dir)
        summary = agg.summarise()
        assert summary.total_runs == 5

    def test_summarise_totals_tokens_and_cost(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        seed_metrics(collector, [
            self._make_entry(tokens=1000, cost=0.01),
            self._make_entry(tokens=2000, cost=0.02),
            self._make_entry(tokens=3000, cost=0.03),
        ])
        agg = MetricsAggregator(logs_dir)
        summary = agg.summarise()
        assert summary.total_tokens == 6000
        assert abs(summary.total_cost_usd - 0.06) < 0.0001

    def test_pass_rate_computed_correctly(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        seed_metrics(collector, [
            self._make_entry(status="pass"),
            self._make_entry(status="pass"),
            self._make_entry(status="fail"),
            self._make_entry(status="needs_human"),
        ])
        agg = MetricsAggregator(logs_dir)
        summary = agg.summarise()
        assert abs(summary.overall_pass_rate - 0.5) < 0.001
        assert abs(summary.overall_failure_rate - 0.25) < 0.001
        assert abs(summary.overall_needs_human_rate - 0.25) < 0.001

    def test_per_agent_returns_agent_metrics(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        seed_metrics(collector, [
            self._make_entry(agent="BureauAgent", latency=2.0),
            self._make_entry(agent="BureauAgent", latency=3.0),
            self._make_entry(agent="FraudAgent", latency=1.0),
        ])
        agg = MetricsAggregator(logs_dir)
        bureau = agg.per_agent("BureauAgent")
        assert bureau is not None
        assert bureau.run_count == 2
        assert bureau.p50_latency > 0

    def test_per_agent_returns_none_for_unknown_agent(self, logs_dir):
        agg = MetricsAggregator(logs_dir)
        assert agg.per_agent("NonExistentAgent") is None

    def test_latency_percentiles_computed(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        latencies = [1.0, 2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 30.0, 40.0, 50.0]
        for lat in latencies:
            seed_metrics(collector, [self._make_entry(latency=lat)])
        agg = MetricsAggregator(logs_dir)
        m = agg.per_agent("A")
        assert m.p50_latency < m.p95_latency < m.p99_latency

    def test_degrading_agents_detected(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        # 10 high-confidence runs then 10 low-confidence runs
        for _ in range(10):
            seed_metrics(collector, [self._make_entry(confidence=0.9)])
        for _ in range(10):
            seed_metrics(collector, [self._make_entry(confidence=0.4)])
        agg = MetricsAggregator(logs_dir)
        summary = agg.summarise()
        assert "A" in summary.degrading_agents

    def test_cost_by_phase(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        seed_metrics(collector, [
            self._make_entry(phase="requirements", cost=0.01),
            self._make_entry(phase="requirements", cost=0.02),
            self._make_entry(phase="testing", cost=0.005),
        ])
        agg = MetricsAggregator(logs_dir)
        by_phase = agg.cost_by_phase()
        assert "requirements" in by_phase
        assert "testing" in by_phase
        assert abs(by_phase["requirements"] - 0.03) < 0.0001

    def test_confidence_over_time_returns_tuples(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        for conf in [0.8, 0.85, 0.9]:
            seed_metrics(collector, [self._make_entry(agent="X", confidence=conf)])
        agg = MetricsAggregator(logs_dir)
        history = agg.confidence_over_time("X")
        assert len(history) == 3
        assert all(isinstance(ts, str) and isinstance(conf, float)
                   for ts, conf in history)

    def test_most_expensive_agents_sorted(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        seed_metrics(collector, [
            self._make_entry(agent="Cheap", cost=0.001),
            self._make_entry(agent="Expensive", cost=1.0),
            self._make_entry(agent="Medium", cost=0.1),
        ])
        agg = MetricsAggregator(logs_dir)
        summary = agg.summarise()
        names = [n for n, _ in summary.most_expensive_agents]
        assert names.index("Expensive") < names.index("Medium")
        assert names.index("Medium") < names.index("Cheap")

    def test_health_score_between_zero_and_one(self, logs_dir):
        collector = MetricsCollector(logs_dir)
        seed_metrics(collector, [
            self._make_entry(status="pass", confidence=0.9, latency=1.0),
            self._make_entry(status="fail", confidence=0.3, latency=25.0),
        ])
        agg = MetricsAggregator(logs_dir)
        m = agg.per_agent("A")
        score = m.health_score()
        assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# _percentile and _compute_trend helpers
# ---------------------------------------------------------------------------

class TestHelpers:

    def test_percentile_p50_of_sorted_list(self):
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _percentile(data, 50) == pytest.approx(3.0, abs=0.1)

    def test_percentile_p100_is_max(self):
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _percentile(data, 100) == 5.0

    def test_percentile_empty_returns_zero(self):
        assert _percentile([], 50) == 0.0

    def test_percentile_single_element(self):
        assert _percentile([7.0], 95) == 7.0

    def test_trend_improving(self):
        data = [0.5] * 10 + [0.9] * 10
        assert _compute_trend(data) == "improving"

    def test_trend_degrading(self):
        data = [0.9] * 10 + [0.4] * 10
        assert _compute_trend(data) == "degrading"

    def test_trend_stable(self):
        data = [0.8] * 20
        assert _compute_trend(data) == "stable"

    def test_trend_insufficient_data(self):
        data = [0.8] * 15  # < 2 * window (20)
        assert _compute_trend(data) == "insufficient_data"


# ---------------------------------------------------------------------------
# BudgetMonitor tests
# ---------------------------------------------------------------------------

class TestBudgetMonitor:

    def _make_entry_obj(self, total_tokens=100, cost_usd=0.01, agent="A") -> MetricsEntry:
        return MetricsEntry(
            agent_name=agent, phase="dev", model_id="m", provider="anthropic",
            input_tokens=total_tokens, output_tokens=0, latency_seconds=1.0,
            status="pass", confidence=0.9, cost_usd=cost_usd, run_id="x",
        )

    def _make_summary(self, total_cost=0.0, per_agent_costs: dict = None):
        from harness.observability.aggregator import HarnessMetricsSummary, AgentMetrics
        summary = HarnessMetricsSummary(total_cost_usd=total_cost)
        for name, cost in (per_agent_costs or {}).items():
            m = AgentMetrics(agent_name=name, phase="dev")
            m.total_cost_usd = cost
            m.aggregate()
            summary.per_agent[name] = m
        return summary

    def test_no_alerts_when_no_budgets(self):
        monitor = BudgetMonitor({})
        entry = self._make_entry_obj(total_tokens=999_999, cost_usd=999.0)
        alerts = monitor.check_run(entry)
        assert alerts == []

    def test_token_alert_when_over_threshold(self):
        monitor = BudgetMonitor({"alert_per_run_tokens": 100})
        entry = self._make_entry_obj(total_tokens=150)
        alerts = monitor.check_run(entry)
        assert len(alerts) == 1
        assert alerts[0].metric == "tokens"
        assert "150" in alerts[0].message

    def test_no_token_alert_when_under_threshold(self):
        monitor = BudgetMonitor({"alert_per_run_tokens": 1000})
        entry = self._make_entry_obj(total_tokens=50)
        assert monitor.check_run(entry) == []

    def test_cost_alert_when_over_threshold(self):
        monitor = BudgetMonitor({"alert_per_run_cost_usd": 0.05})
        entry = self._make_entry_obj(cost_usd=0.10)
        alerts = monitor.check_run(entry)
        assert len(alerts) == 1
        assert alerts[0].metric == "cost_usd"

    def test_multiple_alerts_in_one_run(self):
        monitor = BudgetMonitor({
            "alert_per_run_tokens": 10,
            "alert_per_run_cost_usd": 0.001,
        })
        entry = self._make_entry_obj(total_tokens=100, cost_usd=0.10)
        alerts = monitor.check_run(entry)
        assert len(alerts) == 2

    def test_daily_cost_alert(self):
        monitor = BudgetMonitor({"alert_daily_cost_usd": 10.0})
        summary = self._make_summary(total_cost=15.0)
        alerts = monitor.check_summary(summary)
        assert len(alerts) == 1
        assert alerts[0].metric == "cost_usd"

    def test_critical_level_when_2x_over_daily(self):
        monitor = BudgetMonitor({"alert_daily_cost_usd": 10.0})
        summary = self._make_summary(total_cost=25.0)  # > 2x threshold
        alerts = monitor.check_summary(summary)
        assert any(a.level == "critical" for a in alerts)

    def test_per_agent_cost_alert(self):
        monitor = BudgetMonitor({"alert_per_agent_cost_usd": 1.0})
        summary = self._make_summary(per_agent_costs={"ExpensiveAgent": 5.0, "CheapAgent": 0.1})
        alerts = monitor.check_summary(summary)
        agent_names = [a.agent_name for a in alerts]
        assert "ExpensiveAgent" in agent_names
        assert "CheapAgent" not in agent_names

    def test_budget_alert_str_representation(self):
        alert = BudgetAlert(
            level="warn", message="Too many tokens", agent_name="A",
            metric="tokens", value=150.0, threshold=100.0
        )
        s = str(alert)
        assert "BUDGET" in s
        assert "WARN" in s
        assert "Too many tokens" in s

    def test_critical_alert_uses_red_icon(self):
        alert = BudgetAlert(
            level="critical", message="Way over budget", agent_name="A",
            metric="cost_usd", value=100.0, threshold=10.0
        )
        s = str(alert)
        assert "CRITICAL" in s


# ---------------------------------------------------------------------------
# HarnessConfig observability integration tests
# ---------------------------------------------------------------------------

class TestConfigObservabilityIntegration:

    def test_observability_config_returns_empty_when_no_file(self, config):
        result = config.observability_config()
        assert isinstance(result, dict)

    def test_observability_config_loads_yaml(self, tmp_repo, config):
        import yaml
        obs = {
            "budgets": {"alert_per_run_tokens": 5000},
            "pricing": {"my-model": {"input": 1.0, "output": 2.0}},
        }
        (tmp_repo / "observability_config.yaml").write_text(yaml.dump(obs))
        result = config.observability_config()
        assert result["budgets"]["alert_per_run_tokens"] == 5000
        assert result["pricing"]["my-model"]["input"] == 1.0

    def test_metrics_collector_factory(self, config):
        collector = config.metrics_collector()
        assert isinstance(collector, MetricsCollector)
        assert collector.logs_dir == config.logs_dir


# ---------------------------------------------------------------------------
# BaseAgent.execute() auto-records metrics
# ---------------------------------------------------------------------------

class TestBaseAgentMetricsWiring:

    def test_execute_writes_to_metrics_log(self, config):
        from harness.agents.base_agent import BaseAgent, AgentResult

        class DummyAgent(BaseAgent):
            phase = "testing"
            def run(self, input_data):
                return AgentResult(
                    agent_name=self.name, phase=self.phase,
                    status="pass", output={}, confidence=0.9,
                )

        mock_model = MagicMock()
        mock_model.call_with_fallback.return_value = MagicMock(
            text="{}", model="m", provider="anthropic",
            input_tokens=10, output_tokens=5, latency_seconds=0.1
        )

        with patch("harness.model.build_model", return_value=mock_model):
            with patch("harness.model.prompt_registry.PromptRegistry"):
                agent = DummyAgent(config)
                agent._model = mock_model

        agent.execute({})

        metrics_path = config.logs_dir / "metrics_log.jsonl"
        assert metrics_path.exists()
        entries = [json.loads(l) for l in metrics_path.read_text().splitlines() if l.strip()]
        assert len(entries) == 1
        assert entries[0]["agent_name"] == "DummyAgent"
        assert entries[0]["status"] == "pass"
        assert "latency_seconds" in entries[0]

    def test_execute_records_run_id(self, config):
        from harness.agents.base_agent import BaseAgent, AgentResult

        class DummyAgent(BaseAgent):
            phase = "testing"
            def run(self, input_data):
                return AgentResult(
                    agent_name=self.name, phase=self.phase,
                    status="pass", output={}, confidence=0.9,
                )

        mock_model = MagicMock()
        mock_model.call_with_fallback.return_value = MagicMock(
            text="{}", model="m", provider="anthropic",
            input_tokens=0, output_tokens=0, latency_seconds=0.0
        )

        with patch("harness.model.build_model", return_value=mock_model):
            with patch("harness.model.prompt_registry.PromptRegistry"):
                agent = DummyAgent(config)
                agent._model = mock_model

        agent.execute({})

        metrics_path = config.logs_dir / "metrics_log.jsonl"
        entry = json.loads(metrics_path.read_text().strip())
        assert "run_id" in entry
        assert len(entry["run_id"]) > 0
