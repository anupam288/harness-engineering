"""
dev_agent.py — Phase 3: Development & Coding

The DevAgent is the base for all specialist agents in the development phase.
It enforces the layered architecture rules and provides helpers for agents
that generate code or structured outputs.

Usage: Subclass DevAgent for each specialist in your domain.
The harness wires them together through the OrchestratorAgent.

Example specialist agents to build for your domain:
  - class BureauAgent(DevAgent): ...      # credit bureau queries
  - class FraudAgent(DevAgent): ...       # fraud signal analysis
  - class PolicyAgent(DevAgent): ...      # policy rule evaluation

The StructuralLinter (run in CI) enforces that Layer 1 agents (subclasses
of DevAgent) never import each other. All cross-agent calls go through
the OrchestratorAgent.
"""

from __future__ import annotations

import json

from harness.agents.base_agent import AgentResult, BaseAgent
from harness.constraints.validators import PolicyLinter, SchemaValidator


class DevAgent(BaseAgent):
    """
    Base class for all Phase 3 specialist agents.

    Adds two pre-flight checks that run before the LLM call:
      1. SchemaValidator  — input must conform to policies/agent_schema.json
      2. PolicyLinter     — deterministic hard rules must pass

    Subclasses implement _run_domain_logic(input_data, context) → AgentResult.
    """

    phase = "development"

    def run(self, input_data: dict) -> AgentResult:
        # Pre-flight 1: Schema validation
        schema_path = self.config.policies_dir / "agent_schema.json"
        if schema_path.exists():
            validator = SchemaValidator(schema_path)
            schema_result = validator.validate(input_data)
            if not schema_result.passed:
                return AgentResult(
                    agent_name=self.name,
                    phase=self.phase,
                    status="fail",
                    output={"schema_violations": schema_result.violations},
                    confidence=0.0,
                    flags=[f"schema_violation: {v}" for v in schema_result.violations],
                )

        # Pre-flight 2: Policy linter (deterministic rules only)
        policy_path = self.config.policies_dir / "policy.yaml"
        if policy_path.exists():
            linter = PolicyLinter(policy_path)
            lint_result = linter.lint(input_data)
            if not lint_result.passed:
                return AgentResult(
                    agent_name=self.name,
                    phase=self.phase,
                    status="fail",
                    output={"policy_violations": lint_result.violations},
                    confidence=0.0,
                    flags=[f"policy_violation: {v}" for v in lint_result.violations],
                )

        # Build context and delegate to domain logic
        context = self.build_context(
            extra_docs=["requirements.md", "architecture.md"]
        )
        return self._run_domain_logic(input_data, context)

    def _run_domain_logic(self, input_data: dict, context: str) -> AgentResult:
        """
        Override this in your specialist agent.
        By the time this is called, schema + policy checks have already passed.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement _run_domain_logic()"
        )


class OrchestratorAgent(BaseAgent):
    """
    Layer 2: Merges outputs from parallel specialist agents.
    Resolves conflicts using conflict_policy.yaml.
    Records all conflicts to conflict_log.

    This is the ONLY agent allowed to receive outputs from multiple
    Layer 1 agents and combine them into a single decision.
    """

    phase = "development"

    def run(self, input_data: dict) -> AgentResult:
        """
        input_data keys:
          - agent_results: list of AgentResult dicts from Layer 1 agents
          - input_id: str (identifier for the original input)
        """
        agent_results = input_data.get("agent_results", [])
        input_id = input_data.get("input_id", "unknown")

        if not agent_results:
            return AgentResult(
                agent_name=self.name,
                phase=self.phase,
                status="fail",
                output={"error": "No agent results provided to orchestrator"},
                confidence=0.0,
                flags=["no_agent_results"],
            )

        context = self.build_context(extra_docs=["architecture.md"])

        prompt = f"""
{context}

=== TASK ===
You are the OrchestratorAgent. You have received outputs from multiple
parallel specialist agents. Your job is to:

1. Identify any conflicts between agent outputs
2. Resolve conflicts using the conflict_policy.yaml rules above
3. Produce a single merged decision

Agent results:
{json.dumps(agent_results, indent=2)}

Input ID: {input_id}

Return JSON with keys:
1. "merged_decision" — the single unified output (domain-specific dict)
2. "conflicts_detected" — list of {{"agent_a": str, "agent_b": str,
   "conflict_description": str, "resolution": str, "resolved_by": str}}
3. "final_status" — "pass" | "fail" | "needs_human"
4. "confidence" — float 0.0-1.0 (lower if conflicts were hard to resolve)
5. "reasoning" — 2-3 sentence explanation of the decision

Return ONLY valid JSON.
"""
        try:
            response = self._call_llm(prompt)
            parsed = json.loads(response)

            # Record any conflicts to conflict_log
            conflicts = parsed.get("conflicts_detected", [])
            if conflicts:
                from harness.logs.conflict_log import ConflictLog
                conflict_log = ConflictLog(self.config.logs_dir)
                for conflict in conflicts:
                    conflict_log.record(
                        input_id=input_id,
                        agent_a=conflict.get("agent_a", "unknown"),
                        output_a={},
                        agent_b=conflict.get("agent_b", "unknown"),
                        output_b={},
                        resolution=conflict.get("resolution", ""),
                        resolved_by=conflict.get("resolved_by", "conflict_policy.yaml"),
                    )

            confidence = float(parsed.get("confidence", 0.7))
            status = parsed.get("final_status", "pass")
            if confidence < self.config.confidence_threshold and status == "pass":
                status = "needs_human"

            return AgentResult(
                agent_name=self.name,
                phase=self.phase,
                status=status,
                output={
                    "merged_decision": parsed.get("merged_decision", {}),
                    "conflicts_resolved": len(conflicts),
                    "reasoning": parsed.get("reasoning", ""),
                },
                confidence=confidence,
                flags=[f"conflict_resolved: {c['agent_a']}_vs_{c['agent_b']}"
                       for c in conflicts],
            )

        except Exception as exc:
            return AgentResult(
                agent_name=self.name, phase=self.phase, status="fail",
                output={"error": str(exc)}, confidence=0.0, flags=["llm_call_failed"],
            )
