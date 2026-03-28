"""
qa_agent.py — Phase 4: Testing & QA

Three QA agents, each with a different testing mandate:

QAAgent         — runs happy path + regression scenarios from test_scenarios.yaml
ScenarioAgent   — generates new edge cases from requirements + architecture
AdversarialAgent — actively tries to violate harness constraints

All three write to:
  - docs/edge_cases.md        (open items block deployment gate)
  - docs/quality.md           (per-agent confidence scores)
  - .harness/logs/decision_log.jsonl (via base class)
"""

from __future__ import annotations

import json

import yaml

from harness.agents.base_agent import AgentResult, BaseAgent


class QAAgent(BaseAgent):
    """Runs test scenarios and checks outputs against policy rules."""

    phase = "testing"

    def run(self, input_data: dict) -> AgentResult:
        scenarios_path = self.config.repo_root / "tests" / "scenarios" / "test_scenarios.yaml"
        scenarios = []
        if scenarios_path.exists():
            scenarios = yaml.safe_load(scenarios_path.read_text()) or []

        context = self.build_context(extra_docs=["requirements.md", "architecture.md"])

        prompt = f"""
{context}

=== TASK ===
You are the QAAgent. Run the following test scenarios and evaluate each one.

Scenarios:
{json.dumps(scenarios, indent=2)}

For each scenario, determine:
- Does the expected output match what the harness policies would produce?
- Are any hard rules violated?
- Is the scenario still valid given current requirements?

Return JSON with keys:
1. "results" — list of {{"scenario_id": str, "status": "pass"|"fail"|"stale",
   "notes": str}}
2. "regression_failures" — list of scenario_ids that now fail
3. "stale_scenarios" — list of scenario_ids that no longer apply
4. "confidence" — float 0.0-1.0

Return ONLY valid JSON.
"""
        try:
            response = self._call_llm(prompt)
            parsed = json.loads(response)

            failures = parsed.get("regression_failures", [])
            stale = parsed.get("stale_scenarios", [])
            confidence = float(parsed.get("confidence", 0.7))
            status = "pass" if not failures and confidence >= self.config.confidence_threshold else "fail"

            self._update_quality_md("QAAgent", confidence)

            return AgentResult(
                agent_name=self.name, phase=self.phase, status=status,
                output={"regression_failures": failures, "stale_scenarios": stale,
                        "total_scenarios": len(scenarios)},
                confidence=confidence,
                artifacts_produced=["docs/quality.md"],
                flags=[f"regression_fail: {f}" for f in failures],
            )
        except Exception as exc:
            return AgentResult(
                agent_name=self.name, phase=self.phase, status="fail",
                output={"error": str(exc)}, confidence=0.0, flags=["llm_call_failed"],
            )

    def _update_quality_md(self, agent_name: str, confidence: float) -> None:
        quality_path = self.config.docs_dir / "quality.md"
        entry = f"\n| {agent_name} | {confidence:.2f} | {self._now()} |"
        if not quality_path.exists():
            quality_path.write_text(
                "# quality.md — Per-Agent Confidence Scores\n\n"
                "| Agent | Confidence | Last Updated |\n"
                "|-------|------------|-------------|"
            )
        with quality_path.open("a") as f:
            f.write(entry)

    def _now(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class ScenarioAgent(BaseAgent):
    """Generates new edge case scenarios from requirements and architecture."""

    phase = "testing"

    def run(self, input_data: dict) -> AgentResult:
        context = self.build_context(extra_docs=["requirements.md", "architecture.md"])
        existing_scenarios_path = (
            self.config.repo_root / "tests" / "scenarios" / "test_scenarios.yaml"
        )
        existing = []
        if existing_scenarios_path.exists():
            existing = yaml.safe_load(existing_scenarios_path.read_text()) or []

        prompt = f"""
{context}

=== TASK ===
You are the ScenarioAgent. Generate NEW edge case test scenarios
that are NOT already covered by the existing scenarios below.

Existing scenarios (do not duplicate):
{json.dumps([s.get("id") for s in existing], indent=2)}

Focus on:
- Boundary conditions (values at exactly the threshold)
- Missing or null fields
- Conflicting signals between agents
- Unusual but valid combinations

Return JSON with keys:
1. "new_scenarios" — list of {{"id": str, "description": str,
   "input": dict, "expected_output": dict, "tests_constraint": str}}
2. "edge_cases_md" — markdown content for docs/edge_cases.md.
   Use checkbox format: "- [ ] " for open items needing investigation.
3. "confidence" — float 0.0-1.0

Return ONLY valid JSON.
"""
        try:
            response = self._call_llm(prompt)
            parsed = json.loads(response)

            # Append new scenarios to test_scenarios.yaml
            new_scenarios = parsed.get("new_scenarios", [])
            all_scenarios = existing + new_scenarios
            existing_scenarios_path.parent.mkdir(parents=True, exist_ok=True)
            existing_scenarios_path.write_text(yaml.dump(all_scenarios, default_flow_style=False))

            # Write edge_cases.md
            self.write_artifact("edge_cases.md", parsed.get("edge_cases_md", ""))

            confidence = float(parsed.get("confidence", 0.7))

            return AgentResult(
                agent_name=self.name, phase=self.phase,
                status="pass" if confidence >= self.config.confidence_threshold else "needs_human",
                output={"new_scenarios_generated": len(new_scenarios)},
                confidence=confidence,
                artifacts_produced=["tests/scenarios/test_scenarios.yaml", "docs/edge_cases.md"],
            )
        except Exception as exc:
            return AgentResult(
                agent_name=self.name, phase=self.phase, status="fail",
                output={"error": str(exc)}, confidence=0.0, flags=["llm_call_failed"],
            )


class AdversarialAgent(BaseAgent):
    """
    Actively tries to violate harness constraints.
    Any constraint it breaks must be fixed in the harness before deployment.
    """

    phase = "testing"

    def run(self, input_data: dict) -> AgentResult:
        context = self.build_context(extra_docs=["architecture.md"])

        prompt = f"""
{context}

=== TASK ===
You are the AdversarialAgent. Your job is to find gaps in the harness.
Try to construct inputs or sequences of agent calls that would:
- Allow a hard policy rule to be violated
- Cause the orchestrator to produce an incorrect resolution
- Bypass a schema validation check
- Produce a confident output with insufficient signal

For each gap found, describe:
- What the gap is
- How it could be exploited
- What harness change would close it (new linter rule, policy addition, schema constraint)

Return JSON with keys:
1. "gaps_found" — list of {{"gap_id": str, "description": str,
   "exploit": str, "harness_fix": str}}
2. "harness_gaps_md" — markdown appended to docs/edge_cases.md.
   Each gap as a checkbox: "- [ ] [ADVERSARIAL] gap_id: description"
3. "confidence" — float 0.0-1.0 (your confidence in thoroughness)

Return ONLY valid JSON.
"""
        try:
            response = self._call_llm(prompt)
            parsed = json.loads(response)

            gaps = parsed.get("gaps_found", [])

            # Append to edge_cases.md
            if gaps:
                self.append_to_artifact("edge_cases.md", parsed.get("harness_gaps_md", ""))

            confidence = float(parsed.get("confidence", 0.7))
            # If gaps found, status is fail — deployment is blocked
            status = "fail" if gaps else "pass"

            return AgentResult(
                agent_name=self.name, phase=self.phase, status=status,
                output={"gaps_found": len(gaps), "gap_ids": [g["gap_id"] for g in gaps]},
                confidence=confidence,
                artifacts_produced=["docs/edge_cases.md"],
                flags=[f"harness_gap: {g['gap_id']}" for g in gaps],
            )
        except Exception as exc:
            return AgentResult(
                agent_name=self.name, phase=self.phase, status="fail",
                output={"error": str(exc)}, confidence=0.0, flags=["llm_call_failed"],
            )
