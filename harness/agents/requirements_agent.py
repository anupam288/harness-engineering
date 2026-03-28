"""
requirements_agent.py — Phase 1: Requirements & Planning

Reads raw inputs (spec docs, regulatory PDFs, user stories) and
produces structured harness artifacts:
  - docs/requirements.md
  - docs/uncertain_terms.md  (ambiguities flagged for human resolution)
  - policies/agent_schema.json (v0, field list extracted from requirements)

Gate produced: requirements.md committed → design phase can open.
"""

from __future__ import annotations

from harness.agents.base_agent import AgentResult, BaseAgent
from harness.agents.self_review_agent import ReviewCriteria
from harness.config import HarnessConfig


class RequirementsAgent(BaseAgent):
    phase = "requirements"

    def run(self, input_data: dict) -> AgentResult:
        """
        input_data keys:
          - raw_inputs: list of str (spec text, doc excerpts)
          - project_name: str
          - domain: str (e.g. "loan origination", "inventory management")
        """
        raw_inputs = input_data.get("raw_inputs", [])
        project_name = input_data.get("project_name", "unnamed-project")
        domain = input_data.get("domain", "generic")

        context = self.build_context()

        # Build the LLM prompt
        prompt = f"""
{context}

=== TASK ===
You are the RequirementsAgent for the SDLC harness.
Project: {project_name}
Domain: {domain}

Raw inputs provided:
{chr(10).join(f'---\\n{inp}' for inp in raw_inputs)}

Produce THREE outputs as JSON with keys:
1. "requirements_md" — a structured requirements.md document (markdown string).
   Include sections: Overview, Functional Requirements, Non-Functional Requirements,
   Constraints, Out of Scope.

2. "uncertain_terms" — list of dicts with keys "term" and "question".
   Each entry is an ambiguous term or rule that needs human resolution
   before the design phase can proceed. Be aggressive — flag anything unclear.
   Format as markdown checklist items: "- [ ] term: question"

3. "agent_schema_v0" — a JSON schema dict (object with "properties" and "required")
   describing the minimum input fields any agent in this domain needs.

4. "confidence" — float 0.0-1.0 reflecting how complete the requirements are.

Return ONLY valid JSON. No preamble, no markdown fences.
"""

        try:
            response = self._call_llm(prompt)
            import json
            parsed = json.loads(response)

            # Write harness artifacts
            self.write_artifact("requirements.md", parsed["requirements_md"])

            uncertain_content = f"# Uncertain Terms — resolve before design phase\n\n"
            for item in parsed.get("uncertain_terms", []):
                uncertain_content += f"- [ ] **{item['term']}**: {item['question']}\n"
            self.write_artifact("uncertain_terms.md", uncertain_content)

            import json as _json
            schema_path = self.config.policies_dir / "agent_schema.json"
            schema_path.write_text(_json.dumps(parsed.get("agent_schema_v0", {}), indent=2))

            confidence = float(parsed.get("confidence", 0.7))
            status = "pass" if confidence >= self.config.confidence_threshold else "needs_human"

            return AgentResult(
                agent_name=self.name,
                phase=self.phase,
                status=status,
                output={"project": project_name, "domain": domain,
                        "uncertain_terms_count": len(parsed.get("uncertain_terms", []))},
                confidence=confidence,
                artifacts_produced=["docs/requirements.md", "docs/uncertain_terms.md",
                                    "policies/agent_schema.json"],
                flags=[f"uncertain_term: {t['term']}" for t in parsed.get("uncertain_terms", [])],
            )

        except Exception as exc:
            return AgentResult(
                agent_name=self.name,
                phase=self.phase,
                status="fail",
                output={"error": str(exc)},
                confidence=0.0,
                flags=["llm_call_failed"],
            )

    @property
    def _default_review_criteria(self):
        from harness.agents.self_review_agent import ReviewCriteria
        return ReviewCriteria(
            check_policy_compliance=True,
            check_completeness=True,
            check_json_validity=True,
            check_confidence_calibration=True,
            check_no_hallucination=True,
            custom_checks=[
                "requirements_md must contain all five sections: Overview, Functional Requirements, Non-Functional Requirements, Constraints, Out of Scope",
                "Every uncertain_term entry must have both term and question keys",
                "agent_schema_v0 must have at least one required field",
            ],
        )
