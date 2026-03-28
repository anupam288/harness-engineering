"""
budget.py — BudgetMonitor

Checks token and cost thresholds from observability_config.yaml.
Emits warnings only — never blocks an agent run.

Thresholds checked:
  - alert_per_run_tokens      warn when a single run exceeds N tokens
  - alert_per_run_cost_usd    warn when a single run exceeds $X
  - alert_daily_cost_usd      warn when cumulative daily cost exceeds $X
  - alert_per_agent_cost_usd  warn when any single agent's cumulative cost exceeds $X

All thresholds are optional. Unset = no alert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.observability.metrics import MetricsEntry
    from harness.observability.aggregator import HarnessMetricsSummary


@dataclass
class BudgetAlert:
    level: str       # "warn" | "critical"
    message: str
    agent_name: str
    metric: str      # "tokens" | "cost_usd"
    value: float
    threshold: float
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __str__(self) -> str:
        icon = "🔴" if self.level == "critical" else "⚠"
        return f"{icon} BUDGET {self.level.upper()}: {self.message}"


class BudgetMonitor:
    """
    Evaluates budget thresholds and returns BudgetAlert objects.
    Never raises. Never blocks. Warn-only.

    Usage:
        monitor = BudgetMonitor(budgets_config)
        alerts = monitor.check_run(metrics_entry)
        alerts += monitor.check_summary(harness_summary)
        for alert in alerts:
            print(alert)
    """

    def __init__(self, budgets: dict):
        self.budgets = budgets or {}

    def check_run(self, entry: "MetricsEntry") -> list[BudgetAlert]:
        """Check per-run thresholds against a freshly recorded MetricsEntry."""
        alerts = []

        run_token_limit = self.budgets.get("alert_per_run_tokens")
        if run_token_limit and entry.total_tokens >= run_token_limit:
            alerts.append(BudgetAlert(
                level="warn",
                message=(
                    f"{entry.agent_name} used {entry.total_tokens:,} tokens "
                    f"(threshold: {run_token_limit:,})"
                ),
                agent_name=entry.agent_name,
                metric="tokens",
                value=float(entry.total_tokens),
                threshold=float(run_token_limit),
            ))

        run_cost_limit = self.budgets.get("alert_per_run_cost_usd")
        if run_cost_limit and entry.cost_usd >= run_cost_limit:
            alerts.append(BudgetAlert(
                level="warn",
                message=(
                    f"{entry.agent_name} cost ${entry.cost_usd:.4f} "
                    f"(threshold: ${run_cost_limit:.4f})"
                ),
                agent_name=entry.agent_name,
                metric="cost_usd",
                value=entry.cost_usd,
                threshold=float(run_cost_limit),
            ))

        return alerts

    def check_summary(self, summary: "HarnessMetricsSummary") -> list[BudgetAlert]:
        """Check aggregate thresholds against the full harness summary."""
        alerts = []

        daily_limit = self.budgets.get("alert_daily_cost_usd")
        if daily_limit and summary.total_cost_usd >= daily_limit:
            alerts.append(BudgetAlert(
                level="critical" if summary.total_cost_usd >= daily_limit * 2 else "warn",
                message=(
                    f"Total cumulative cost ${summary.total_cost_usd:.4f} "
                    f"≥ daily threshold ${daily_limit:.4f}"
                ),
                agent_name="harness",
                metric="cost_usd",
                value=summary.total_cost_usd,
                threshold=float(daily_limit),
            ))

        per_agent_limit = self.budgets.get("alert_per_agent_cost_usd")
        if per_agent_limit:
            for agent_name, agent_metrics in summary.per_agent.items():
                if agent_metrics.total_cost_usd >= per_agent_limit:
                    alerts.append(BudgetAlert(
                        level="warn",
                        message=(
                            f"{agent_name} cumulative cost "
                            f"${agent_metrics.total_cost_usd:.4f} "
                            f"≥ threshold ${per_agent_limit:.4f}"
                        ),
                        agent_name=agent_name,
                        metric="cost_usd",
                        value=agent_metrics.total_cost_usd,
                        threshold=float(per_agent_limit),
                    ))

        return alerts
