"""
gate.py — Phase gate checker for the SDLC harness.

Gates enforce that each phase only opens when the previous phase
produced its required harness artifacts. Gates are the mechanism
that makes the harness a real SDLC system, not just a collection
of agents.

Usage:
    gate = PhaseGate(config)
    result = gate.check("design")
    if not result.passed:
        print(result.report())
        sys.exit(1)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from harness.config import HarnessConfig


@dataclass
class GateResult:
    phase: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def report(self) -> str:
        lines = [
            f"Phase gate: {self.phase.upper()}",
            f"Status:     {'✓ OPEN' if self.passed else '✗ BLOCKED'}",
        ]
        if self.failures:
            lines.append("\nBlockers:")
            for f in self.failures:
                lines.append(f"  ✗ {f}")
        if self.warnings:
            lines.append("\nWarnings:")
            for w in self.warnings:
                lines.append(f"  ⚠ {w}")
        return "\n".join(lines)


class PhaseGate:
    """
    Checks whether a given SDLC phase is allowed to open.

    Three checks per phase:
      1. Required docs exist in docs/
      2. Required policy files exist in policies/
      3. Zero open items in specified docs (looks for [ ] checkbox markers)
    """

    OPEN_ITEM_PATTERN = re.compile(r"^\s*-\s*\[\s*\]\s+", re.MULTILINE)

    def __init__(self, config: HarnessConfig):
        self.config = config

    def check(self, phase: str) -> GateResult:
        gate_spec = self.config.phase_gates.get(phase)
        if gate_spec is None:
            return GateResult(
                phase=phase,
                passed=False,
                failures=[f"Unknown phase '{phase}'. Valid phases: {list(self.config.phase_gates)}"],
            )

        failures = []
        warnings = []

        # Check 1: required docs exist
        for doc_name in gate_spec.get("required_docs", []):
            doc_path = self.config.docs_dir / doc_name
            if not doc_path.exists():
                failures.append(f"Missing required doc: docs/{doc_name}")
            elif doc_path.stat().st_size < 50:
                warnings.append(f"docs/{doc_name} exists but appears nearly empty")

        # Check 2: required policies exist
        for policy_name in gate_spec.get("required_policies", []):
            found = any(
                (self.config.policies_dir / f"{policy_name}{ext}").exists()
                for ext in (".yaml", ".yml", ".json")
            )
            if not found:
                failures.append(f"Missing required policy: policies/{policy_name}.yaml")

        # Check 3: zero open items in specified docs
        for doc_name in gate_spec.get("zero_open_items_in", []):
            doc_path = self.config.docs_dir / doc_name
            if doc_path.exists():
                content = doc_path.read_text()
                open_items = self.OPEN_ITEM_PATTERN.findall(content)
                if open_items:
                    failures.append(
                        f"docs/{doc_name} has {len(open_items)} open item(s) "
                        f"— resolve all before opening this phase"
                    )
            # If the doc doesn't exist, it was already caught in check 1

        passed = len(failures) == 0
        return GateResult(phase=phase, passed=passed, failures=failures, warnings=warnings)

    def check_all(self) -> dict[str, GateResult]:
        """Run gate checks for all phases. Useful for status dashboards."""
        return {phase: self.check(phase) for phase in self.config.phase_gates}

    def assert_open(self, phase: str) -> None:
        """
        Assert the gate is open. Raises RuntimeError if blocked.
        Use at the start of each phase runner when strict mode is on.
        """
        if not self.config.phase_gates_strict:
            return
        result = self.check(phase)
        if not result.passed:
            raise RuntimeError(
                f"Phase gate BLOCKED for '{phase}':\n{result.report()}"
            )
