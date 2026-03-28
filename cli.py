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

    args = parser.parse_args()
    config = HarnessConfig.from_repo(args.repo)

    dispatch = {
        "gate": cmd_gate,
        "run": cmd_run,
        "gc": cmd_gc,
        "status": cmd_status,
    }

    sys.exit(dispatch[args.command](args, config))


if __name__ == "__main__":
    main()
