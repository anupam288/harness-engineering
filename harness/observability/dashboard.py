"""
dashboard.py — HarnessDashboard

Renders a rich terminal dashboard from aggregated metrics + live gate status.
Pure Python — no external dependencies beyond what's already in requirements.txt.

Invoked via: python cli.py dashboard
             python cli.py dashboard --agent RequirementsAgent
             python cli.py dashboard --watch   (refresh every 30s)
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.config import HarnessConfig
    from harness.observability.aggregator import HarnessMetricsSummary


# Terminal colour codes (degrade gracefully if unsupported)
_R = "\033[31m"   # red
_Y = "\033[33m"   # yellow
_G = "\033[32m"   # green
_B = "\033[36m"   # cyan
_W = "\033[37m"   # white
_D = "\033[2m"    # dim
_BOLD = "\033[1m"
_RESET = "\033[0m"


def _c(text: str, colour: str) -> str:
    return f"{colour}{text}{_RESET}"


def _bar(value: float, width: int = 20, char: str = "█", empty: str = "░") -> str:
    """Render a simple ASCII progress bar."""
    filled = round(value * width)
    filled = max(0, min(width, filled))
    colour = _G if value >= 0.8 else (_Y if value >= 0.5 else _R)
    return _c(char * filled, colour) + _c(empty * (width - filled), _D)


def _trend_icon(trend: str) -> str:
    return {"improving": _c("↑", _G), "degrading": _c("↓", _R),
            "stable": _c("→", _W), "insufficient_data": _c("~", _D)}.get(trend, "?")


def _status_icon(status_or_rate: float | str) -> str:
    if isinstance(status_or_rate, str):
        return {
            "pass": _c("✓", _G), "fail": _c("✗", _R),
            "needs_human": _c("⚠", _Y), "open": _c("✓", _G),
            "blocked": _c("✗", _R),
        }.get(status_or_rate, "?")
    # Float pass rate
    if status_or_rate >= 0.9:
        return _c("✓", _G)
    if status_or_rate >= 0.7:
        return _c("⚠", _Y)
    return _c("✗", _R)


class HarnessDashboard:
    """
    Renders the full harness observability dashboard to stdout.

    Usage:
        dashboard = HarnessDashboard(config)
        dashboard.render()
        dashboard.render_agent("RequirementsAgent")
        dashboard.watch(interval=30)
    """

    WIDTH = 72

    def __init__(self, config: "HarnessConfig"):
        self.config = config
        self._summary = None

    def _get_summary(self) -> "HarnessMetricsSummary":
        from harness.observability.aggregator import MetricsAggregator
        obs_config = self.config.observability_config()
        agg = MetricsAggregator(self.config.logs_dir, budgets=obs_config.get("budgets", {}))
        return agg.summarise()

    def render(self) -> None:
        """Render the full dashboard to stdout."""
        summary = self._get_summary()
        self._summary = summary

        self._header("SDLC Harness — Observability Dashboard")
        self._render_health(summary)
        self._render_gates()
        self._render_agents(summary)
        self._render_cost(summary)
        self._render_budget_alerts(summary)
        self._footer()

    def render_agent(self, agent_name: str) -> None:
        """Render detailed view for a single agent."""
        summary = self._get_summary()
        agent = summary.per_agent.get(agent_name)

        if not agent:
            print(f"\n  No metrics found for '{agent_name}'.")
            print(f"  Run `python cli.py run <phase>` to generate metrics.\n")
            return

        self._header(f"Agent Detail — {agent_name}")

        # Confidence trend
        from harness.observability.aggregator import MetricsAggregator
        agg = MetricsAggregator(self.config.logs_dir)
        history = agg.confidence_over_time(agent_name, last_n=20)

        self._section("Confidence over last 20 runs")
        if history:
            for ts, conf in history[-10:]:
                bar = _bar(conf, width=30)
                ts_short = ts[11:19]  # HH:MM:SS
                print(f"  {_c(ts_short, _D)}  {bar}  {conf:.2f}")
        else:
            print("  No history yet.")

        self._section("Performance")
        print(f"  Runs:           {agent.run_count}")
        print(f"  Pass rate:      {_bar(agent.pass_rate, 15)}  {agent.pass_rate:.0%}")
        print(f"  Failure rate:   {agent.failure_rate:.0%}")
        print(f"  Needs human:    {agent.needs_human_rate:.0%}")
        print(f"  Confidence:     {agent.mean_confidence:.3f}  {_trend_icon(agent.confidence_trend)}")

        self._section("Latency")
        print(f"  p50:  {agent.p50_latency:.2f}s")
        print(f"  p95:  {agent.p95_latency:.2f}s")
        print(f"  p99:  {agent.p99_latency:.2f}s")

        self._section("Cost")
        print(f"  Total tokens:   {agent.total_tokens:,}")
        print(f"  Total cost:     ${agent.total_cost_usd:.4f}")
        print(f"  Cost / run:     ${agent.total_cost_usd / max(agent.run_count, 1):.4f}")
        if agent.mean_review_iterations > 0:
            print(f"  Avg review its: {agent.mean_review_iterations:.1f}")

        self._footer()

    def watch(self, interval: int = 30) -> None:
        """Refresh the dashboard every `interval` seconds. Ctrl-C to exit."""
        try:
            while True:
                print("\033[2J\033[H", end="")  # clear screen
                self.render()
                print(f"\n  {_c(f'Refreshing every {interval}s — Ctrl-C to exit', _D)}")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n  Dashboard stopped.")

    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------

    def _header(self, title: str) -> None:
        line = "─" * self.WIDTH
        print(f"\n{_c(line, _D)}")
        print(f"  {_c(_BOLD + title + _RESET, _B)}")
        print(f"{_c(line, _D)}")

    def _section(self, title: str) -> None:
        print(f"\n  {_c(title, _BOLD)}")
        print(f"  {'─' * (self.WIDTH - 2)}")

    def _footer(self) -> None:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        print(f"\n{_c('─' * self.WIDTH, _D)}")
        print(f"  {_c(f'Generated at {ts}', _D)}\n")

    def _render_health(self, summary: "HarnessMetricsSummary") -> None:
        self._section("Harness health")
        score = summary.harness_health_score
        colour = _G if score >= 0.8 else (_Y if score >= 0.5 else _R)
        print(f"  Score:          {_bar(score)}  {_c(f'{score:.2f}', colour)}")
        print(f"  Total runs:     {summary.total_runs}")
        print(f"  Pass rate:      {_bar(summary.overall_pass_rate, 15)}  "
              f"{summary.overall_pass_rate:.0%}")
        print(f"  Failure rate:   {summary.overall_failure_rate:.0%}")
        print(f"  Needs human:    {summary.overall_needs_human_rate:.0%}")
        if summary.degrading_agents:
            names = ", ".join(summary.degrading_agents)
            print(f"  {_c('↓ Degrading:', _R)}     {names}")

    def _render_gates(self) -> None:
        from harness.gate import PhaseGate
        self._section("Phase gates")
        gate = PhaseGate(self.config)
        results = gate.check_all()
        for phase, result in results.items():
            icon = _c("✓ OPEN   ", _G) if result.passed else _c("✗ BLOCKED", _R)
            blockers = f"  — {result.failures[0]}" if result.failures else ""
            print(f"  {icon}  {phase:<14}{_c(blockers, _D)}")

    def _render_agents(self, summary: "HarnessMetricsSummary") -> None:
        self._section("Per-agent metrics")
        if not summary.per_agent:
            print("  No agent runs recorded yet.")
            return

        # Header row
        print(f"  {'Agent':<24} {'Runs':>5} {'Pass%':>6} {'Conf':>6} "
              f"{'Trend':>6} {'p95':>7} {'Cost':>8}")
        print(f"  {'─'*24} {'─'*5} {'─'*6} {'─'*6} {'─'*6} {'─'*7} {'─'*8}")

        for name, m in sorted(summary.per_agent.items()):
            trend = _trend_icon(m.confidence_trend)
            pass_pct = f"{m.pass_rate:.0%}"
            conf = f"{m.mean_confidence:.3f}"
            p95 = f"{m.p95_latency:.1f}s"
            cost = f"${m.total_cost_usd:.4f}"
            icon = _status_icon(m.pass_rate)
            print(f"  {icon} {name:<22} {m.run_count:>5} {pass_pct:>6} "
                  f"{conf:>6} {trend:>6} {p95:>7} {cost:>8}")

    def _render_cost(self, summary: "HarnessMetricsSummary") -> None:
        self._section("Cost summary")
        print(f"  Total tokens:   {summary.total_tokens:,}")
        print(f"  Total cost:     ${summary.total_cost_usd:.4f}")

        from harness.observability.aggregator import MetricsAggregator
        agg = MetricsAggregator(self.config.logs_dir)
        by_phase = agg.cost_by_phase()
        if by_phase:
            print(f"\n  By phase:")
            for phase, cost in sorted(by_phase.items(), key=lambda x: x[1], reverse=True):
                print(f"    {phase:<20} ${cost:.4f}")

        if summary.most_expensive_agents:
            print(f"\n  Most expensive agents:")
            for name, cost in summary.most_expensive_agents[:3]:
                print(f"    {name:<24} ${cost:.4f}")

    def _render_budget_alerts(self, summary: "HarnessMetricsSummary") -> None:
        from harness.observability.budget import BudgetMonitor
        obs_config = self.config.observability_config()
        monitor = BudgetMonitor(obs_config.get("budgets", {}))
        alerts = monitor.check_summary(summary)

        if not alerts and not summary.budget_warnings:
            return

        self._section("Budget alerts")
        for alert in alerts:
            print(f"  {alert}")
        for warning in summary.budget_warnings:
            print(f"  ⚠ {_c(warning, _Y)}")
