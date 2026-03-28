"""
test_gates.py — Tests for the PhaseGate system.
test_constraints.py — Tests for SchemaValidator, PolicyLinter, StructuralLinter.

Run with: pytest tests/
"""

import json
import tempfile
from pathlib import Path

import pytest
import yaml

from harness.config import HarnessConfig
from harness.gate import PhaseGate
from harness.constraints.validators import SchemaValidator, PolicyLinter, StructuralLinter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_repo(tmp_path):
    """Create a minimal repo structure for testing."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "policies").mkdir()
    (tmp_path / "harness" / "agents").mkdir(parents=True)
    (tmp_path / ".harness" / "logs").mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("# AGENTS.md\nTest harness.")
    return tmp_path


@pytest.fixture
def config(tmp_repo):
    return HarnessConfig(
        repo_root=tmp_repo,
        logs_dir=tmp_repo / ".harness" / "logs",
        docs_dir=tmp_repo / "docs",
        policies_dir=tmp_repo / "policies",
        phase_gates_strict=True,
    )


# ---------------------------------------------------------------------------
# Gate tests
# ---------------------------------------------------------------------------

class TestPhaseGate:

    def test_requirements_gate_blocked_when_doc_missing(self, config):
        gate = PhaseGate(config)
        result = gate.check("requirements")
        assert not result.passed
        assert any("requirements.md" in f for f in result.failures)

    def test_requirements_gate_passes_when_doc_exists(self, config):
        (config.docs_dir / "requirements.md").write_text(
            "# Requirements\n\nThis is a real requirements doc with enough content."
        )
        gate = PhaseGate(config)
        result = gate.check("requirements")
        assert result.passed

    def test_design_gate_blocked_when_uncertain_terms_open(self, config):
        (config.docs_dir / "requirements.md").write_text("# Requirements\n\nContent here.")
        (config.docs_dir / "architecture.md").write_text("# Architecture\n\nContent here.")
        (config.docs_dir / "uncertain_terms.md").write_text(
            "# Uncertain Terms\n\n- [ ] **term**: This is an open item\n"
        )
        (config.policies_dir / "policy.yaml").write_text("rules: []")
        (config.policies_dir / "conflict_policy.yaml").write_text("conflicts: []")
        (config.policies_dir / "agent_schema.json").write_text("{}")

        gate = PhaseGate(config)
        result = gate.check("design")
        assert not result.passed
        assert any("uncertain_terms.md" in f for f in result.failures)

    def test_design_gate_passes_when_uncertain_terms_resolved(self, config):
        (config.docs_dir / "requirements.md").write_text("# Requirements\n\nContent here.")
        (config.docs_dir / "architecture.md").write_text("# Architecture\n\nContent here.")
        (config.docs_dir / "uncertain_terms.md").write_text(
            "# Uncertain Terms\n\n- [x] **term**: This item is resolved\n"
        )
        (config.policies_dir / "policy.yaml").write_text("rules: []")
        (config.policies_dir / "conflict_policy.yaml").write_text("conflicts: []")
        (config.policies_dir / "agent_schema.json").write_text("{}")

        gate = PhaseGate(config)
        result = gate.check("design")
        assert result.passed

    def test_deployment_gate_blocked_when_edge_cases_open(self, config):
        (config.docs_dir / "edge_cases.md").write_text(
            "# Edge Cases\n\n- [ ] [ADVERSARIAL] GAP_001: A harness gap\n"
        )
        (config.policies_dir / "rollback_triggers.yaml").write_text("thresholds: {}")

        gate = PhaseGate(config)
        result = gate.check("deployment")
        assert not result.passed
        assert any("edge_cases.md" in f for f in result.failures)

    def test_check_all_returns_all_phases(self, config):
        gate = PhaseGate(config)
        results = gate.check_all()
        expected_phases = {"requirements", "design", "development", "testing", "deployment", "monitoring"}
        assert expected_phases == set(results.keys())

    def test_unknown_phase_fails(self, config):
        gate = PhaseGate(config)
        result = gate.check("nonexistent_phase")
        assert not result.passed
        assert any("Unknown phase" in f for f in result.failures)

    def test_assert_open_raises_when_blocked(self, config):
        gate = PhaseGate(config)
        with pytest.raises(RuntimeError, match="BLOCKED"):
            gate.assert_open("requirements")

    def test_assert_open_passes_when_gate_open(self, config):
        (config.docs_dir / "requirements.md").write_text("# Requirements\n\nContent here.")
        gate = PhaseGate(config)
        gate.assert_open("requirements")  # should not raise


# ---------------------------------------------------------------------------
# Schema Validator tests
# ---------------------------------------------------------------------------

class TestSchemaValidator:

    @pytest.fixture
    def schema_path(self, tmp_path):
        schema = {
            "required": ["name", "score", "category"],
            "properties": {
                "name": {"type": "string"},
                "score": {"type": "number", "minimum": 0, "maximum": 1000},
                "category": {"type": "string", "enum": ["A", "B", "C"]},
                "optional_field": {"type": "string"},
            }
        }
        path = tmp_path / "schema.json"
        path.write_text(json.dumps(schema))
        return path

    def test_valid_input_passes(self, schema_path):
        validator = SchemaValidator(schema_path)
        result = validator.validate({"name": "test", "score": 750, "category": "A"})
        assert result.passed

    def test_missing_required_field_fails(self, schema_path):
        validator = SchemaValidator(schema_path)
        result = validator.validate({"name": "test", "score": 750})
        assert not result.passed
        assert any("category" in v for v in result.violations)

    def test_null_required_field_fails(self, schema_path):
        validator = SchemaValidator(schema_path)
        result = validator.validate({"name": None, "score": 750, "category": "A"})
        assert not result.passed

    def test_below_minimum_fails(self, schema_path):
        validator = SchemaValidator(schema_path)
        result = validator.validate({"name": "test", "score": -1, "category": "A"})
        assert not result.passed
        assert any("minimum" in v for v in result.violations)

    def test_above_maximum_fails(self, schema_path):
        validator = SchemaValidator(schema_path)
        result = validator.validate({"name": "test", "score": 1001, "category": "A"})
        assert not result.passed

    def test_invalid_enum_value_fails(self, schema_path):
        validator = SchemaValidator(schema_path)
        result = validator.validate({"name": "test", "score": 500, "category": "X"})
        assert not result.passed
        assert any("allowed values" in v for v in result.violations)

    def test_optional_field_absence_is_warning_not_failure(self, schema_path):
        validator = SchemaValidator(schema_path)
        result = validator.validate({"name": "test", "score": 500, "category": "B"})
        assert result.passed
        assert any("optional_field" in w for w in result.warnings)

    def test_missing_schema_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            SchemaValidator(tmp_path / "nonexistent.json")


# ---------------------------------------------------------------------------
# Policy Linter tests
# ---------------------------------------------------------------------------

class TestPolicyLinter:

    @pytest.fixture
    def policy_path(self, tmp_path):
        policy = {
            "rules": [
                {
                    "rule_id": "TEST_001",
                    "description": "Score must not exceed 900",
                    "condition": "score > 900",
                    "action": "block",
                    "deterministic": True,
                },
                {
                    "rule_id": "TEST_002",
                    "description": "Ratio warning above 0.5",
                    "condition": "ratio > 0.5",
                    "action": "warn",
                    "deterministic": True,
                },
                {
                    "rule_id": "TEST_003",
                    "description": "LLM-based rule — not enforced by linter",
                    "condition": "",
                    "action": "block",
                    "deterministic": False,
                },
            ]
        }
        path = tmp_path / "policy.yaml"
        path.write_text(yaml.dump(policy))
        return path

    def test_clean_input_passes(self, policy_path):
        linter = PolicyLinter(policy_path)
        result = linter.lint({"score": 750, "ratio": 0.4})
        assert result.passed

    def test_block_rule_violation_fails(self, policy_path):
        linter = PolicyLinter(policy_path)
        result = linter.lint({"score": 950, "ratio": 0.4})
        assert not result.passed
        assert any("TEST_001" in v for v in result.violations)

    def test_warn_rule_violation_is_warning_not_failure(self, policy_path):
        linter = PolicyLinter(policy_path)
        result = linter.lint({"score": 750, "ratio": 0.6})
        assert result.passed  # warn doesn't block
        assert any("TEST_002" in w for w in result.warnings)

    def test_non_deterministic_rule_not_enforced(self, policy_path):
        linter = PolicyLinter(policy_path)
        result = linter.lint({"score": 750, "ratio": 0.4})
        # TEST_003 has deterministic=False — linter ignores it
        assert result.passed
        assert not any("TEST_003" in v for v in result.violations)

    def test_missing_field_in_condition_does_not_crash(self, policy_path):
        linter = PolicyLinter(policy_path)
        result = linter.lint({})  # no fields at all
        assert result.passed  # missing fields are skipped, not crashed on


# ---------------------------------------------------------------------------
# Structural Linter tests
# ---------------------------------------------------------------------------

class TestStructuralLinter:

    def test_clean_agents_pass(self, tmp_repo):
        agents_dir = tmp_repo / "harness" / "agents"

        # Write a clean Layer 1 agent that doesn't import other Layer 1 agents
        (agents_dir / "bureau_agent.py").write_text(
            "from harness.agents.base_agent import BaseAgent\n"
            "class BureauAgent(BaseAgent):\n    pass\n"
        )

        linter = StructuralLinter(agents_dir)
        result = linter.lint()
        assert result.passed

    def test_layer1_importing_layer1_fails(self, tmp_repo):
        agents_dir = tmp_repo / "harness" / "agents"

        # Bureau agent illegally imports fraud agent
        (agents_dir / "bureau_agent.py").write_text(
            "from harness.agents.fraud_agent import FraudAgent\n"
            "class BureauAgent:\n    pass\n"
        )
        (agents_dir / "fraud_agent.py").write_text(
            "class FraudAgent:\n    pass\n"
        )

        linter = StructuralLinter(agents_dir)
        result = linter.lint()
        assert not result.passed
        assert any("bureau_agent" in v and "fraud_agent" in v for v in result.violations)
