"""
architecture_agent.py — Phase 2: Design & Architecture

Reads requirements.md and uncertain_terms.md (must be zero open items)
and produces:
  - docs/architecture.md      (agent map, module boundaries, layer rules)
  - policies/policy.yaml      (domain hard rules)
  - policies/conflict_policy.yaml  (how orchestrator resolves disagreements)

Gate produced: all policy files committed → development phase can open.
"""

from __future__ import annotations

import json

import yaml

from harness.agents.base_agent import AgentResult, BaseAgent


class ArchitectureAgent(BaseAgent):
    phase = "design"

    def run(self, input_data: dict) -> AgentResult:
        context = self.build_context(
            extra_docs=["requirements.md", "uncertain_terms.md"]
        )

        prompt = f"""
{context}

=== TASK ===
You are the ArchitectureAgent for the SDLC harness.

Based on the requirements and resolved uncertain terms above, produce
a JSON object with these keys:

1. "architecture_md" — docs/architecture.md content (markdown).
   Sections: Agent Map, Module Boundaries, Layer Rules, Data Flow,
   Cross-Agent Communication Rules.
   IMPORTANT: Layer 1 agents may NOT call each other directly.
   All cross-agent calls go through Layer 2 (Orchestrator).

2. "policy_yaml" — dict of domain hard rules.
   Each rule: {{"rule_id": str, "description": str, "condition": str,
   "action": "block"|"warn"|"escalate", "deterministic": true|false}}
   Deterministic rules are enforced by linters, not LLM judgment.

3. "conflict_policy_yaml" — dict describing how to resolve agent conflicts.
   Keys: agent pairs (e.g. "bureau_vs_fraud"), value: resolution strategy.

4. "confidence" — float 0.0-1.0.

Return ONLY valid JSON. No preamble, no markdown fences.
"""

        try:
            response = self._call_llm(prompt)
            parsed = json.loads(response)

            self.write_artifact("architecture.md", parsed["architecture_md"])

            policy_path = self.config.policies_dir / "policy.yaml"
            policy_path.write_text(yaml.dump(parsed.get("policy_yaml", {}), default_flow_style=False))

            conflict_path = self.config.policies_dir / "conflict_policy.yaml"
            conflict_path.write_text(yaml.dump(parsed.get("conflict_policy_yaml", {}), default_flow_style=False))

            confidence = float(parsed.get("confidence", 0.7))
            status = "pass" if confidence >= self.config.confidence_threshold else "needs_human"

            return AgentResult(
                agent_name=self.name,
                phase=self.phase,
                status=status,
                output={"policies_written": ["policy.yaml", "conflict_policy.yaml"]},
                confidence=confidence,
                artifacts_produced=["docs/architecture.md", "policies/policy.yaml",
                                    "policies/conflict_policy.yaml"],
            )
        except Exception as exc:
            return AgentResult(
                agent_name=self.name, phase=self.phase, status="fail",
                output={"error": str(exc)}, confidence=0.0, flags=["llm_call_failed"],
            )
