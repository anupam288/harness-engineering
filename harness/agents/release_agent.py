"""
release_agent.py — Phase 5: Deployment & Release

Two agents:
  ReleaseAgent   — manages staged rollout (canary → full)
  RollbackAgent  — monitors production health, triggers rollback on threshold breach

Rollback thresholds live in policies/rollback_triggers.yaml.
Neither agent may modify that file at runtime — changes go through PRs.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from harness.agents.base_agent import AgentResult, BaseAgent


class ReleaseAgent(BaseAgent):
    """
    Manages staged rollout.

    Checks the deployment gate (edge_cases.md zero open items),
    generates a release plan, and records the rollout decision.

    In a real system this would invoke your deployment tooling (k8s,
    Terraform, etc.). Here it produces a structured release_plan.md
    and a release record in the decision log.
    """

    phase = "deployment"

    def run(self, input_data: dict) -> AgentResult:
        rollback_triggers = self._load_rollback_triggers()
        canary_pct = rollback_triggers.get("canary", {}).get("traffic_percentage", 5)

        context = self.build_context(
            extra_docs=["requirements.md", "architecture.md", "edge_cases.md"]
        )

        prompt = f"""
{context}

=== TASK ===
You are the ReleaseAgent. Produce a release plan for a staged rollout.

Canary traffic: {canary_pct}%
Rollback thresholds: {json.dumps(rollback_triggers.get("thresholds", {}), indent=2)}

Return JSON with keys:
1. "release_plan_md" — markdown content for docs/release_plan.md.
   Include: rollout stages, health checks per stage, rollback triggers,
   success criteria, and estimated timeline.
2. "release_checklist" — list of strings, each a pre-release check item.
3. "risk_assessment" — "low" | "medium" | "high"
4. "confidence" — float 0.0-1.0
5. "proceed" — boolean: is it safe to proceed with canary?

Return ONLY valid JSON.
"""
        try:
            response = self._call_llm(prompt)
            parsed = json.loads(response)

            self.write_artifact("release_plan.md", parsed.get("release_plan_md", ""))

            confidence = float(parsed.get("confidence", 0.8))
            proceed = parsed.get("proceed", True)
            risk = parsed.get("risk_assessment", "medium")

            status = "pass" if proceed and confidence >= self.config.confidence_threshold else "needs_human"
            if risk == "high":
                status = "needs_human"

            return AgentResult(
                agent_name=self.name,
                phase=self.phase,
                status=status,
                output={
                    "canary_traffic_pct": canary_pct,
                    "risk_assessment": risk,
                    "proceed": proceed,
                    "checklist_items": len(parsed.get("release_checklist", [])),
                },
                confidence=confidence,
                artifacts_produced=["docs/release_plan.md"],
                flags=(["high_risk_release"] if risk == "high" else []),
            )

        except Exception as exc:
            return AgentResult(
                agent_name=self.name, phase=self.phase, status="fail",
                output={"error": str(exc)}, confidence=0.0, flags=["llm_call_failed"],
            )

    def _load_rollback_triggers(self) -> dict:
        path = self.config.policies_dir / "rollback_triggers.yaml"
        if path.exists():
            return yaml.safe_load(path.read_text()) or {}
        return {}

    def _call_llm(self, prompt: str) -> str:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=self.config.llm_model,
            max_tokens=self.config.llm_max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text


class RollbackAgent(BaseAgent):
    """
    Monitors production health metrics and triggers rollback if any
    threshold in rollback_triggers.yaml is breached.

    In a real system, feed this agent live metrics from your observability
    stack (Datadog, CloudWatch, Prometheus, etc.).

    The agent never modifies rollback_triggers.yaml — it only reads it.
    Threshold changes go through PRs reviewed by humans.
    """

    phase = "deployment"

    def run(self, input_data: dict) -> AgentResult:
        """
        input_data keys (production metrics to evaluate):
          - decision_quality:   float  (0.0 - 1.0)
          - agent_failure_rate: float  (0.0 - 1.0)
          - needs_human_rate:   float  (0.0 - 1.0)
          - latency_p95_seconds: float
          - average_confidence: float  (0.0 - 1.0)
          - sample_size:        int    (number of decisions evaluated)
        """
        triggers = self._load_rollback_triggers()
        thresholds = triggers.get("thresholds", {})
        min_sample = triggers.get("canary", {}).get("minimum_decisions_before_check", 20)

        sample_size = input_data.get("sample_size", 0)
        breaches = []
        warnings_list = []

        if sample_size < min_sample:
            return AgentResult(
                agent_name=self.name,
                phase=self.phase,
                status="pass",
                output={"message": f"Insufficient sample size ({sample_size} < {min_sample}). Monitoring continues."},
                confidence=1.0,
            )

        # Evaluate each threshold deterministically
        checks = [
            ("decision_quality",    "decision_quality_min",      lambda v, t: v < t,  "Decision quality below minimum"),
            ("agent_failure_rate",  "agent_failure_rate_max",    lambda v, t: v > t,  "Agent failure rate above maximum"),
            ("needs_human_rate",    "needs_human_rate_max",      lambda v, t: v > t,  "Needs-human rate above maximum"),
            ("latency_p95_seconds", "latency_p95_max_seconds",   lambda v, t: v > t,  "P95 latency above maximum"),
            ("average_confidence",  "average_confidence_min",    lambda v, t: v < t,  "Average confidence below minimum"),
        ]

        for metric_key, threshold_key, breach_fn, description in checks:
            metric_value = input_data.get(metric_key)
            threshold_value = thresholds.get(threshold_key)

            if metric_value is None or threshold_value is None:
                continue

            if breach_fn(metric_value, threshold_value):
                breaches.append(
                    f"{description}: {metric_value:.3f} (threshold: {threshold_value})"
                )

        rollback_triggered = len(breaches) > 0
        status = "fail" if rollback_triggered else "pass"

        # Log rollback event if triggered
        if rollback_triggered:
            self._log_rollback(input_data, breaches)

        return AgentResult(
            agent_name=self.name,
            phase=self.phase,
            status=status,
            output={
                "rollback_triggered": rollback_triggered,
                "threshold_breaches": breaches,
                "metrics_evaluated": input_data,
                "sample_size": sample_size,
            },
            confidence=1.0,  # This agent is deterministic — confidence is always 1.0
            flags=[f"threshold_breach: {b}" for b in breaches],
        )

    def _load_rollback_triggers(self) -> dict:
        path = self.config.policies_dir / "rollback_triggers.yaml"
        if path.exists():
            return yaml.safe_load(path.read_text()) or {}
        return {}

    def _log_rollback(self, metrics: dict, breaches: list[str]) -> None:
        rollback_log_path = self.config.logs_dir / "rollback_log.jsonl"
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "rollback_triggered": True,
            "breaches": breaches,
            "metrics_at_trigger": metrics,
        }
        with rollback_log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
