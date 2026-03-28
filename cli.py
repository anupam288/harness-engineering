#!/usr/bin/env python3
"""
cli.py — Single entrypoint for the SDLC harness.

Usage:
  python cli.py run <phase> [--input <json_file>]
  python cli.py gate <phase>
  python cli.py gate --all
  python cli.py gc
  python cli.py status

Examples:
  python cli.py gate --all
  python cli.py run requirements --input inputs/my_project.json
  python cli.py run testing
  python cli.py gc
  python cli.py status
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from harness.config import HarnessConfig
from harness.gate import PhaseGate


def cmd_gate(args, config: HarnessConfig) -> int:
    gate = PhaseGate(config)

    if args.all:
        results = gate.check_all()
        print("\n=== SDLC Harness Gate Status ===\n")
        all_passed = True
        for phase, result in results.items():
            icon = "✓" if result.passed else "✗"
            print(f"  {icon} {phase.upper():<20} {'OPEN' if result.passed else 'BLOCKED'}")
            if result.failures:
                for f in result.failures:
                    print(f"      ✗ {f}")
            if result.warnings:
                for w in result.warnings:
                    print(f"      ⚠ {w}")
            if not result.passed:
                all_passed = False
        print()
        return 0 if all_passed else 1

    result = gate.check(args.phase)
    print(result.report())
    return 0 if result.passed else 1


def cmd_run(args, config: HarnessConfig) -> int:
    phase = args.phase
    gate = PhaseGate(config)

    # Check gate before running
    gate_result = gate.check(phase)
    if not gate_result.passed and config.phase_gates_strict:
        print(f"\nGate BLOCKED for phase '{phase}':")
        print(gate_result.report())
        print("\nResolve the blockers above before running this phase.")
        return 1

    if gate_result.warnings:
        print("Warnings:")
        for w in gate_result.warnings:
            print(f"  ⚠ {w}")
        print()

    # Load input data
    input_data = {}
    if args.input:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"Error: input file not found: {args.input}")
            return 1
        input_data = json.loads(input_path.read_text())

    # Route to the right agent(s)
    print(f"\nRunning phase: {phase.upper()}\n")

    if phase == "requirements":
        from harness.agents.requirements_agent import RequirementsAgent
        agent = RequirementsAgent(config)
        result = agent.execute(input_data)
        _print_result(result)

    elif phase == "design":
        from harness.agents.architecture_agent import ArchitectureAgent
        agent = ArchitectureAgent(config)
        result = agent.execute(input_data)
        _print_result(result)

    elif phase == "testing":
        from harness.agents.qa_agent import QAAgent, ScenarioAgent, AdversarialAgent
        print("--- QA Agent ---")
        result = QAAgent(config).execute(input_data)
        _print_result(result)

        print("--- Scenario Agent ---")
        result = ScenarioAgent(config).execute(input_data)
        _print_result(result)

        print("--- Adversarial Agent ---")
        result = AdversarialAgent(config).execute(input_data)
        _print_result(result)

    elif phase == "monitoring":
        from harness.agents.gc_agent import GCAgent
        agent = GCAgent(config)
        result = agent.execute(input_data)
        _print_result(result)

    else:
        print(f"Phase '{phase}' runner not yet implemented.")
        print("Available: requirements, design, testing, monitoring")
        return 1

    return 0 if result.passed() else 1


def cmd_gc(args, config: HarnessConfig) -> int:
    """Run the GC agent directly."""
    from harness.agents.gc_agent import GCAgent
    print("\nRunning GC Agent (nightly harness health check)...\n")
    agent = GCAgent(config)
    result = agent.execute({})
    _print_result(result)
    if result.output.get("prs_proposed", 0) > 0:
        print(f"\n{result.output['prs_proposed']} PR(s) proposed in .harness/proposed_prs/")
        print("Review and merge them to improve the harness.")
    return 0 if result.passed() else 1





def cmd_security(args, config) -> int:
    """Security audit commands."""
    sub = getattr(args, "security_sub", None)

    if sub == "audit" or sub is None:
        # Full security audit: secrets scan + log verification
        print("\n=== Harness Security Audit ===\n")
        exit_code = 0

        # 1. Secrets scan
        from harness.security.secrets_scanner import SecretsScanner
        print("Scanning for hardcoded secrets...")
        scanner = SecretsScanner(skip_test_files=True)
        scan_result = scanner.scan_directory(config.repo_root / "harness")
        print(scan_result.report())
        if not scan_result.passed:
            exit_code = 1

        # 2. Log integrity
        print("\nVerifying decision log integrity...")
        from harness.logs.decision_log import DecisionLog
        log = DecisionLog(config.logs_dir)
        results = log.verify_integrity()
        if not results:
            print("  Log signing not configured (set HARNESS_LOG_SIGNING_KEY to enable)")
        else:
            from harness.security.log_signer import LogVerifier
            verifier = LogVerifier.from_env()
            if verifier:
                print(verifier.summary(results))
                if any(not r.valid for r in results):
                    exit_code = 1

        print()
        return exit_code

    elif sub == "scan-secrets":
        from harness.security.secrets_scanner import SecretsScanner
        target = pathlib.Path(getattr(args, "path", "."))
        scanner = SecretsScanner(
            skip_test_files=not getattr(args, "include_tests", False)
        )
        result = scanner.scan_directory(target)
        print(result.report())
        return 0 if result.passed else 1

    elif sub == "verify-logs":
        from harness.security.log_signer import LogVerifier
        from harness.logs.decision_log import DecisionLog
        verifier = LogVerifier.from_env()
        if verifier is None:
            print("\n  HARNESS_LOG_SIGNING_KEY not set — log verification skipped.\n")
            return 0
        log = DecisionLog(config.logs_dir)
        results = log.verify_integrity()
        print(f"\n{verifier.summary(results)}\n")
        return 0 if all(r.valid for r in results) else 1

    else:
        print(f"Unknown security subcommand: {sub}")
        print("Available: audit, scan-secrets, verify-logs")
        return 1


def cmd_monitor(args, config) -> int:
    """Run the log monitoring pipeline."""
    import yaml
    from harness.monitoring.adapters import build_adapters_from_config
    from harness.monitoring.ingestor import LogIngestor
    from harness.monitoring.log_monitor_agent import LogMonitorAgent

    mon_config_path = config.repo_root / "monitoring_config.yaml"
    if not mon_config_path.exists():
        print("\n  monitoring_config.yaml not found.")
        print("  Copy the template from the repo and configure your log sources.\n")
        return 1

    mon_config = yaml.safe_load(mon_config_path.read_text()) or {}
    adapters = build_adapters_from_config(mon_config)

    enabled = [a for a in adapters if a.enabled]
    if not enabled:
        print("\n  No adapters enabled in monitoring_config.yaml.")
        print("  Set enabled: true for at least one adapter.\n")
        return 1

    agent = LogMonitorAgent(config)
    ingestor = LogIngestor(enabled, mon_config.get("ingestor", {}))

    serve = getattr(args, "serve", False)
    poll = getattr(args, "poll", False)
    adapter_name = getattr(args, "adapter", None)
    health = getattr(args, "health", False)

    if health:
        print("\n=== Adapter health check ===\n")
        results = ingestor.health_check_all()
        for name, (ok, msg) in results.items():
            icon = "✓" if ok else "✗"
            print(f"  {icon} {name}: {msg}")
        print()
        return 0

    if serve:
        # Start webhook server and run polling loop
        from harness.monitoring.adapters.webhook_adapter import WebhookAdapter
        for adapter in enabled:
            if isinstance(adapter, WebhookAdapter):
                adapter.start_server(daemon=True)
        print("\n  Webhook server started. Running polling loop (Ctrl-C to stop)...")
        try:
            ingestor.run_forever(on_window=agent.analyse)
        except KeyboardInterrupt:
            print("\n  Monitor stopped.")
        return 0

    if poll:
        print(f"\n  Starting polling loop (Ctrl-C to stop)...")
        try:
            ingestor.run_forever(on_window=agent.analyse)
        except KeyboardInterrupt:
            print("\n  Monitor stopped.")
        return 0

    # Default: single triggered run
    print("\n  Running triggered log analysis...\n")
    ingestor.run_once(on_window=agent.analyse)
    return 0


def cmd_dashboard(args, config) -> int:
    """Render the observability dashboard."""
    from harness.observability.dashboard import HarnessDashboard
    dashboard = HarnessDashboard(config)
    agent = getattr(args, "agent", None)
    watch = getattr(args, "watch", False)
    if agent:
        dashboard.render_agent(agent)
    elif watch:
        obs = config.observability_config()
        interval = obs.get("dashboard_refresh_seconds", 30)
        dashboard.watch(interval=interval)
    else:
        dashboard.render()
    return 0


def cmd_metrics(args, config) -> int:
    """Print a metrics summary table."""
    from harness.observability.aggregator import MetricsAggregator
    from harness.observability.budget import BudgetMonitor

    obs = config.observability_config()
    agg = MetricsAggregator(config.logs_dir, budgets=obs.get("budgets", {}))
    summary = agg.summarise()

    if summary.total_runs == 0:
        print("\nNo metrics recorded yet. Run a phase first.\n")
        return 0

    print("\n=== Harness Metrics Summary ===")
    print(f"Total runs:    {summary.total_runs}")
    print(f"Total tokens:  {summary.total_tokens:,}")
    print(f"Total cost:    ${summary.total_cost_usd:.4f}")
    print(f"Health score:  {summary.harness_health_score:.2f}")
    print(f"Pass rate:     {summary.overall_pass_rate:.0%}")
    print(f"Failure rate:  {summary.overall_failure_rate:.0%}")
    print(f"Needs human:   {summary.overall_needs_human_rate:.0%}")

    if summary.degrading_agents:
        print(f"\nDegrading agents: {', '.join(summary.degrading_agents)}")

    print("\nPer-agent breakdown:")
    print(f"  {'Agent':<26} {'Runs':>5} {'Pass%':>6} {'p95':>7} {'Cost':>9}")
    print(f"  {'─'*26} {'─'*5} {'─'*6} {'─'*7} {'─'*9}")
    for name, m in sorted(summary.per_agent.items()):
        print(f"  {name:<26} {m.run_count:>5} {m.pass_rate:>6.0%} "
              f"{m.p95_latency:>6.1f}s ${m.total_cost_usd:>8.4f}")

    monitor = BudgetMonitor(obs.get("budgets", {}))
    alerts = monitor.check_summary(summary)
    if alerts:
        print("\nBudget alerts:")
        for alert in alerts:
            print(f"  {alert}")
    print()
    return 0


def cmd_status(args, config: HarnessConfig) -> int:
    """Show harness health status."""
    print("\n=== SDLC Harness Status ===\n")
    print(config.summary())

    # Gate overview
    gate = PhaseGate(config)
    results = gate.check_all()
    open_count = sum(1 for r in results.values() if r.passed)
    print(f"Phase gates: {open_count}/{len(results)} open\n")

    # Log stats
    from harness.logs.decision_log import DecisionLog
    log = DecisionLog(config.logs_dir)
    all_decisions = log.read_all()
    failures = log.read_failures()
    needs_human = log.read_needs_human()

    print(f"Decision log: {len(all_decisions)} total")
    print(f"  Failures:     {len(failures)}")
    print(f"  Needs human:  {len(needs_human)}")

    # Proposed PRs
    pr_dir = config.repo_root / ".harness" / "proposed_prs"
    if pr_dir.exists():
        prs = list(pr_dir.glob("*.md"))
        print(f"\nProposed PRs: {len(prs)} awaiting review")
        for pr in sorted(prs)[-5:]:
            print(f"  • {pr.name}")

    print()
    return 0


def _print_result(result) -> None:
    icon = "✓" if result.passed() else ("⚠" if result.needs_human() else "✗")
    print(f"{icon} {result.agent_name} [{result.status.upper()}] confidence={result.confidence:.2f}")
    if result.artifacts_produced:
        for a in result.artifacts_produced:
            print(f"  → {a}")
    if result.flags:
        for f in result.flags:
            print(f"  ⚑ {f}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="SDLC Harness CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--repo", default=".", help="Path to repo root (default: current dir)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # gate
    gate_parser = subparsers.add_parser("gate", help="Check phase gate(s)")
    gate_parser.add_argument("phase", nargs="?", help="Phase name to check")
    gate_parser.add_argument("--all", action="store_true", help="Check all phases")

    # run
    run_parser = subparsers.add_parser("run", help="Run a phase")
    run_parser.add_argument("phase", help="Phase to run")
    run_parser.add_argument("--input", help="Path to JSON input file")

    # gc
    subparsers.add_parser("gc", help="Run the GC agent")

    # status
    subparsers.add_parser("status", help="Show harness health status")

    # security
    sec_parser = subparsers.add_parser("security", help="Security audit commands")
    sec_parser.add_argument("security_sub", nargs="?",
                            choices=["audit", "scan-secrets", "verify-logs"],
                            default="audit")
    sec_parser.add_argument("--path", default=".", help="Directory to scan")
    sec_parser.add_argument("--include-tests", action="store_true",
                            help="Include test files in secrets scan")

    # monitor
    mon_parser = subparsers.add_parser("monitor", help="Run log monitoring pipeline")
    mon_parser.add_argument("--poll", action="store_true", help="Polling mode (continuous)")
    mon_parser.add_argument("--serve", action="store_true", help="Start webhook server + poll")
    mon_parser.add_argument("--adapter", help="Filter to one adapter")
    mon_parser.add_argument("--health", action="store_true", help="Check adapter connectivity")

    # dashboard
    dash_parser = subparsers.add_parser("dashboard", help="Observability dashboard")
    dash_parser.add_argument("--agent", help="Detail view for one agent")
    dash_parser.add_argument("--watch", action="store_true", help="Auto-refresh")

    # metrics
    subparsers.add_parser("metrics", help="Metrics summary table")

    args = parser.parse_args()
    config = HarnessConfig.from_repo(args.repo)

    dispatch = {
        "gate": cmd_gate,
        "run": cmd_run,
        "gc": cmd_gc,
        "status": cmd_status,
        "dashboard": cmd_dashboard,
        "metrics": cmd_metrics,
        "monitor": cmd_monitor,
        "security": cmd_security,
    }

    sys.exit(dispatch[args.command](args, config))


if __name__ == "__main__":
    main()
