"""
aggregator.py — MetricsAggregator

Reads metrics_log.jsonl and computes all derived metrics used by
the dashboard and the `python cli.py metrics` summary command.

Computed per-agent:
  - p50 / p95 / p99 latency
  - mean confidence + trend (improving / degrading / stable)
  - failure rate, needs_human rate
  - total tokens, total cost (USD)
  - review iteration distribution

Computed globally:
  - cumulative cost across all agents and runs
  - harness health score (composite of failure rate + confidence + gate status)
  - most expensive agents
  - slowest agents (p95 latency)
  - agents with degrading confidence (last 10 runs vs prior 10)
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AgentMetrics:
    """Aggregated metrics for one agent."""
    agent_name: str
    phase: str
    run_count: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    latencies: list[float] = field(default_factory=list)
    confidences: list[float] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    review_iterations: list[int] = field(default_factory=list)
    models_used: set = field(default_factory=set)

    # Derived (computed by aggregate())
    p50_latency: float = 0.0
    p95_latency: float = 0.0
    p99_latency: float = 0.0
    mean_confidence: float = 0.0
    confidence_trend: str = "stable"   # "improving" | "degrading" | "stable" | "insufficient_data"
    failure_rate: float = 0.0
    needs_human_rate: float = 0.0
    pass_rate: float = 0.0
    mean_review_iterations: float = 0.0

    def aggregate(self) -> None:
        """Compute all derived fields from raw lists."""
        if not self.latencies:
            return

        sorted_lat = sorted(self.latencies)
        n = len(sorted_lat)
        self.p50_latency = _percentile(sorted_lat, 50)
        self.p95_latency = _percentile(sorted_lat, 95)
        self.p99_latency = _percentile(sorted_lat, 99)

        if self.confidences:
            self.mean_confidence = statistics.mean(self.confidences)
            self.confidence_trend = _compute_trend(self.confidences)

        if self.statuses:
            total = len(self.statuses)
            self.failure_rate = self.statuses.count("fail") / total
            self.needs_human_rate = self.statuses.count("needs_human") / total
            self.pass_rate = self.statuses.count("pass") / total

        if self.review_iterations:
            reviewed = [i for i in self.review_iterations if i > 0]
            self.mean_review_iterations = statistics.mean(reviewed) if reviewed else 0.0

    def health_score(self) -> float:
        """
        0.0 – 1.0 composite health for this agent.
        Weights: pass_rate 40%, mean_confidence 40%, low_latency 20%.
        """
        latency_score = max(0.0, 1.0 - (self.p95_latency / 30.0))  # 30s = 0.0
        return round(
            0.4 * self.pass_rate +
            0.4 * self.mean_confidence +
            0.2 * latency_score,
            3,
        )


@dataclass
class HarnessMetricsSummary:
    """Global aggregated view of the entire harness."""
    total_runs: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    overall_pass_rate: float = 0.0
    overall_failure_rate: float = 0.0
    overall_needs_human_rate: float = 0.0
    harness_health_score: float = 0.0
    per_agent: dict[str, AgentMetrics] = field(default_factory=dict)
    most_expensive_agents: list[tuple[str, float]] = field(default_factory=list)
    slowest_agents: list[tuple[str, float]] = field(default_factory=list)
    degrading_agents: list[str] = field(default_factory=list)
    budget_warnings: list[str] = field(default_factory=list)


class MetricsAggregator:
    """
    Reads metrics_log.jsonl and computes all derived metrics.

    Usage:
        agg = MetricsAggregator(logs_dir)
        summary = agg.summarise()
        agent_metrics = agg.per_agent("RequirementsAgent")
    """

    def __init__(self, logs_dir: Path, budgets: dict = None):
        self.metrics_path = logs_dir / "metrics_log.jsonl"
        self.budgets = budgets or {}
        self._entries: list[dict] = []
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        if not self.metrics_path.exists():
            self._entries = []
            self._loaded = True
            return
        lines = self.metrics_path.read_text().splitlines()
        self._entries = [
            __import__("json").loads(line)
            for line in lines if line.strip()
        ]
        self._loaded = True

    def summarise(self) -> HarnessMetricsSummary:
        """Compute the full global summary."""
        self._load()
        summary = HarnessMetricsSummary()

        if not self._entries:
            return summary

        # Build per-agent buckets
        buckets: dict[str, AgentMetrics] = defaultdict(
            lambda: AgentMetrics(agent_name="", phase="")
        )
        all_statuses = []

        for e in self._entries:
            name = e.get("agent_name", "unknown")
            bucket = buckets[name]
            bucket.agent_name = name
            bucket.phase = e.get("phase", "unknown")
            bucket.run_count += 1
            bucket.total_tokens += e.get("total_tokens", 0)
            bucket.total_cost_usd += e.get("cost_usd", 0.0)
            bucket.latencies.append(e.get("latency_seconds", 0.0))
            bucket.confidences.append(e.get("confidence", 0.0))
            bucket.statuses.append(e.get("status", "unknown"))
            bucket.review_iterations.append(e.get("review_iterations", 0))
            model = e.get("model_id", "unknown")
            bucket.models_used.add(model)
            all_statuses.append(e.get("status", "unknown"))

        for bucket in buckets.values():
            bucket.aggregate()

        summary.total_runs = len(self._entries)
        summary.total_tokens = sum(e.get("total_tokens", 0) for e in self._entries)
        summary.total_cost_usd = round(sum(e.get("cost_usd", 0.0) for e in self._entries), 6)
        summary.per_agent = dict(buckets)

        if all_statuses:
            total = len(all_statuses)
            summary.overall_pass_rate = all_statuses.count("pass") / total
            summary.overall_failure_rate = all_statuses.count("fail") / total
            summary.overall_needs_human_rate = all_statuses.count("needs_human") / total

        # Composite harness health (average of per-agent health scores)
        scores = [b.health_score() for b in buckets.values()]
        summary.harness_health_score = round(statistics.mean(scores), 3) if scores else 0.0

        # Rankings
        summary.most_expensive_agents = sorted(
            [(name, b.total_cost_usd) for name, b in buckets.items()],
            key=lambda x: x[1], reverse=True
        )[:5]

        summary.slowest_agents = sorted(
            [(name, b.p95_latency) for name, b in buckets.items()],
            key=lambda x: x[1], reverse=True
        )[:5]

        summary.degrading_agents = [
            name for name, b in buckets.items()
            if b.confidence_trend == "degrading"
        ]

        # Budget warnings
        daily_limit = self.budgets.get("alert_daily_cost_usd")
        if daily_limit and summary.total_cost_usd >= daily_limit:
            summary.budget_warnings.append(
                f"Daily cost ${summary.total_cost_usd:.4f} ≥ threshold ${daily_limit:.4f}"
            )

        return summary

    def per_agent(self, agent_name: str) -> AgentMetrics | None:
        """Return aggregated metrics for a single agent."""
        summary = self.summarise()
        return summary.per_agent.get(agent_name)

    def recent_runs(self, n: int = 20) -> list[dict]:
        """Return the N most recent raw metric entries."""
        self._load()
        return self._entries[-n:] if self._entries else []

    def cost_by_phase(self) -> dict[str, float]:
        """Total cost grouped by SDLC phase."""
        self._load()
        by_phase: dict[str, float] = defaultdict(float)
        for e in self._entries:
            by_phase[e.get("phase", "unknown")] += e.get("cost_usd", 0.0)
        return {k: round(v, 6) for k, v in sorted(by_phase.items())}

    def confidence_over_time(self, agent_name: str, last_n: int = 30) -> list[tuple[str, float]]:
        """Return (timestamp, confidence) pairs for an agent over the last N runs."""
        self._load()
        runs = [
            (e["timestamp"], e["confidence"])
            for e in self._entries
            if e.get("agent_name") == agent_name
        ]
        return runs[-last_n:]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _percentile(sorted_data: list[float], pct: int) -> float:
    if not sorted_data:
        return 0.0
    n = len(sorted_data)
    idx = (pct / 100) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return round(sorted_data[lo] * (1 - frac) + sorted_data[hi] * frac, 3)


def _compute_trend(confidences: list[float], window: int = 10) -> str:
    """
    Compare the last `window` runs against the prior `window` runs.
    Returns "improving", "degrading", "stable", or "insufficient_data".
    """
    if len(confidences) < window * 2:
        return "insufficient_data"
    recent = statistics.mean(confidences[-window:])
    prior = statistics.mean(confidences[-window * 2:-window])
    delta = recent - prior
    if delta > 0.05:
        return "improving"
    if delta < -0.05:
        return "degrading"
    return "stable"
