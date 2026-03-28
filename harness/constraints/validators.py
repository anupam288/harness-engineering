"""
schema_validator.py — Deterministic input validation.
policy_linter.py   — Deterministic policy rule enforcement.
structural_linter.py — Deterministic layer architecture enforcement.

These run BEFORE any LLM call. Hard rules are never delegated to LLMs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Shared result type
# ---------------------------------------------------------------------------

@dataclass
class LintResult:
    passed: bool
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def report(self) -> str:
        lines = ["✓ PASSED" if self.passed else "✗ FAILED"]
        for v in self.violations:
            lines.append(f"  ✗ {v}")
        for w in self.warnings:
            lines.append(f"  ⚠ {w}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Schema Validator
# ---------------------------------------------------------------------------

class SchemaValidator:
    """
    Validates agent input against policies/agent_schema.json.
    Also runs InputSanitiser to detect prompt injection.
    Runs before any agent processes input.
    No LLM involved — pure deterministic validation.
    """

    def __init__(self, schema_path: Path, security_config: dict = None):
        if not schema_path.exists():
            raise FileNotFoundError(f"Schema not found: {schema_path}")
        self.schema = json.loads(schema_path.read_text())
        self._security_cfg = (security_config or {}).get("sanitiser", {})

    def validate(self, input_data: dict) -> LintResult:
        # Run prompt injection sanitiser first
        from harness.security.sanitiser import InputSanitiser
        sanitiser = InputSanitiser(self._security_cfg)
        san_result = sanitiser.sanitise(input_data)
        if not san_result.passed:
            return LintResult(
                passed=False,
                violations=[
                    f"[SECURITY] {i.field_name}: {i.issue_type} ({i.pattern_label}): {i.excerpt}"
                    for i in san_result.issues if i.severity == "block"
                ],
                warnings=[
                    f"[SECURITY] {i.field_name}: {i.issue_type} ({i.pattern_label})"
                    for i in san_result.issues if i.severity == "warn"
                ],
            )
        # Use sanitised input for remaining checks (strips control chars etc.)
        input_data = san_result.sanitised

        violations = []
        warnings = []

        required = self.schema.get("required", [])
        properties = self.schema.get("properties", {})

        # Check required fields
        for field_name in required:
            if field_name not in input_data:
                violations.append(f"Missing required field: '{field_name}'")
            elif input_data[field_name] is None:
                violations.append(f"Required field '{field_name}' is null")

        # Check types and constraints
        for field_name, field_schema in properties.items():
            if field_name not in input_data:
                if field_name not in required:
                    warnings.append(f"Optional field '{field_name}' not provided")
                continue

            value = input_data[field_name]
            expected_type = field_schema.get("type")

            if expected_type == "number" and not isinstance(value, (int, float)):
                violations.append(f"Field '{field_name}' must be a number, got {type(value).__name__}")

            if expected_type == "string" and not isinstance(value, str):
                violations.append(f"Field '{field_name}' must be a string, got {type(value).__name__}")

            if "minimum" in field_schema and isinstance(value, (int, float)):
                if value < field_schema["minimum"]:
                    violations.append(
                        f"Field '{field_name}' value {value} below minimum {field_schema['minimum']}"
                    )

            if "maximum" in field_schema and isinstance(value, (int, float)):
                if value > field_schema["maximum"]:
                    violations.append(
                        f"Field '{field_name}' value {value} above maximum {field_schema['maximum']}"
                    )

            if "enum" in field_schema and value not in field_schema["enum"]:
                violations.append(
                    f"Field '{field_name}' value '{value}' not in allowed values: {field_schema['enum']}"
                )

        return LintResult(passed=len(violations) == 0, violations=violations, warnings=warnings)


# ---------------------------------------------------------------------------
# Policy Linter
# ---------------------------------------------------------------------------

class PolicyLinter:
    """
    Enforces hard policy rules from policies/policy.yaml.
    Only rules marked deterministic=true are enforced here.
    LLM-based rules are handled by the relevant agent.
    """

    def __init__(self, policy_path: Path):
        if not policy_path.exists():
            raise FileNotFoundError(f"Policy not found: {policy_path}")
        raw = yaml.safe_load(policy_path.read_text()) or {}
        self.rules = raw.get("rules", [])

    def lint(self, input_data: dict) -> LintResult:
        violations = []
        warnings = []

        deterministic_rules = [r for r in self.rules if r.get("deterministic", False)]

        for rule in deterministic_rules:
            rule_id = rule.get("rule_id", "unknown")
            condition = rule.get("condition", "")
            action = rule.get("action", "warn")

            try:
                # Evaluate condition safely against input_data
                violated = self._evaluate_condition(condition, input_data)
            except Exception as exc:
                warnings.append(f"Rule '{rule_id}' could not be evaluated: {exc}")
                continue

            if violated:
                message = f"Rule '{rule_id}': {rule.get('description', condition)}"
                if action == "block":
                    violations.append(message)
                elif action == "warn":
                    warnings.append(message)
                elif action == "escalate":
                    warnings.append(f"[ESCALATE] {message}")

        return LintResult(passed=len(violations) == 0, violations=violations, warnings=warnings)

    def _evaluate_condition(self, condition: str, data: dict) -> bool:
        """
        Evaluate a condition string against input data.
        Conditions are simple expressions like:
          "foir > 0.5"
          "credit_score < 700"
          "ltv > 0.8"

        Only supports: field_name operator value
        Operators: >, <, >=, <=, ==, !=
        """
        import operator as op

        ops = {
            ">": op.gt, "<": op.lt,
            ">=": op.ge, "<=": op.le,
            "==": op.eq, "!=": op.ne,
        }

        condition = condition.strip()

        for op_str in [">=", "<=", "!=", ">", "<", "=="]:
            if op_str in condition:
                parts = condition.split(op_str, 1)
                if len(parts) == 2:
                    field = parts[0].strip()
                    threshold_str = parts[1].strip()
                    if field not in data:
                        return False
                    try:
                        value = float(data[field])
                        threshold = float(threshold_str)
                        return ops[op_str](value, threshold)
                    except (ValueError, TypeError):
                        return False

        return False


# ---------------------------------------------------------------------------
# Structural Linter
# ---------------------------------------------------------------------------

class StructuralLinter:
    """
    Enforces the layer architecture rule:
    Layer 1 agents may not import from each other.
    All cross-agent calls must go through Layer 2 (Orchestrator).

    Scans Python files in the harness/agents/ directory.
    """

    LAYER_1_AGENTS = [
        "requirements_agent",
        "bureau_agent",
        "fraud_agent",
        "policy_agent",
    ]

    LAYER_2_AGENTS = [
        "orchestrator_agent",
        "gc_agent",
    ]

    def __init__(self, agents_dir: Path):
        self.agents_dir = agents_dir

    def lint(self) -> LintResult:
        violations = []
        warnings = []

        # Secrets scan — run across entire agents directory
        from harness.security.secrets_scanner import SecretsScanner
        scanner = SecretsScanner(skip_test_files=True)
        scan_result = scanner.scan_directory(self.agents_dir)
        for finding in scan_result.findings:
            msg = (f"Possible hardcoded secret in {finding.file_path}:{finding.line_number} "
                   f"({finding.pattern_label}): {finding.excerpt}")
            if finding.severity == "critical":
                violations.append(msg)
            else:
                warnings.append(msg)

        for agent_file in self.agents_dir.glob("*.py"):
            agent_name = agent_file.stem
            if agent_name not in self.LAYER_1_AGENTS:
                continue

            source = agent_file.read_text()

            # Check if this Layer 1 agent imports any other Layer 1 agent
            for other_agent in self.LAYER_1_AGENTS:
                if other_agent == agent_name:
                    continue
                if f"from harness.agents.{other_agent}" in source or \
                   f"import {other_agent}" in source:
                    violations.append(
                        f"Layer violation: '{agent_name}' imports '{other_agent}'. "
                        f"Layer 1 agents may not call each other directly. "
                        f"Route through the Orchestrator (Layer 2)."
                    )

            # Check if this agent writes to policy files
            if "policies_dir" in source and ".write_text(" in source:
                warnings.append(
                    f"'{agent_name}' may be writing to policy files at runtime. "
                    f"Policy changes must go through PRs only."
                )

        return LintResult(passed=len(violations) == 0, violations=violations, warnings=warnings)
