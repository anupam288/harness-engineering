"""
log_monitor_agent.py — LogMonitorAgent

A harness agent that analyses LogWindows and decides corrective actions.

Reads:
  - monitoring_rules.yaml — versioned error patterns + expected responses
  - The LogWindow (events, error rate, counts)
  - AGENTS.md + policy files (like all harness agents)

Produces:
  - MonitoringDecision (action, severity, root_cause, proposed_fix)

The agent runs in two modes:
  1. Deterministic pre-check — pattern matching against monitoring_rules.yaml
     before any LLM call. Known error patterns with defined actions are handled
     immediately without token cost.
  2. LLM analysis — for novel or ambiguous error patterns not in the rules,
     the agent calls the LLM to classify and propose a fix.

This mirrors the harness constraint principle: deterministic first, LLM only
for what deterministic code cannot handle.
"""

from __future__ import annotations

import json
import re

import yaml

from harness.agents.base_agent import AgentResult, BaseAgent
from harness.config import HarnessConfig
from harness.monitoring.action_runner import ActionRunner, MonitoringDecision, VALID_ACTIONS
from harness.monitoring.log_event import LogLevel, LogWindow


class LogMonitorAgent(BaseAgent):
    """
    Analyses runtime log windows and decides corrective actions.

    Usage:
        agent = LogMonitorAgent(config)
        agent.analyse(window)            # calls action_runner automatically
        result = agent.execute({"window": window.to_dict()})  # BaseAgent path
    """

    phase = "monitoring"

    def __init__(self, config: HarnessConfig):
        super().__init__(config)
        self._rules = self._load_rules()
        self._action_runner = ActionRunner(config)

    def run(self, input_data: dict) -> AgentResult:
        """BaseAgent.execute() path — input_data contains a serialised window."""
        window_dict = input_data.get("window", {})
        from harness.monitoring.log_event import LogEvent, LogWindow as LW
        from datetime import datetime

        events = [LogEvent.from_dict(e) for e in window_dict.get("events", [])]
        window = LW(
            events=events,
            window_start=datetime.fromisoformat(
                window_dict.get("window_start",
                datetime.now().isoformat())
            ),
            window_end=datetime.fromisoformat(
                window_dict.get("window_end",
                datetime.now().isoformat())
            ),
            source=window_dict.get("source", "unknown"),
            total_count=window_dict.get("total_count", len(events)),
            error_count=window_dict.get("error_count", 0),
            warning_count=window_dict.get("warning_count", 0),
            critical_count=window_dict.get("critical_count", 0),
        )

        decision = self._decide(window)
        self._action_runner.execute(decision, window)

        return AgentResult(
            agent_name=self.name,
            phase=self.phase,
            status="pass",
            output=decision.to_dict(),
            confidence=decision.confidence,
            flags=decision.flags,
        )

    def analyse(self, window: LogWindow) -> MonitoringDecision:
        """
        Primary entry point for the monitoring pipeline.
        Analyses a LogWindow and executes the appropriate action.
        """
        decision = self._decide(window)
        self._action_runner.execute(decision, window)
        return decision

    # ------------------------------------------------------------------
    # Decision logic
    # ------------------------------------------------------------------

    def _decide(self, window: LogWindow) -> MonitoringDecision:
        """
        Two-stage decision:
        1. Deterministic rule matching (zero token cost)
        2. LLM analysis for novel/ambiguous patterns
        """
        if not window.events:
            return MonitoringDecision(
                action="log_only", severity="low",
                summary="No log events in window — nothing to analyse.",
                root_cause="Empty window", matched_rules=[], confidence=1.0,
            )

        # Stage 1: Deterministic rule matching
        matched_rules = self._match_rules(window)
        if matched_rules:
            rule = matched_rules[0]   # highest-priority matched rule
            return MonitoringDecision(
                action=rule.get("action", "log_only"),
                severity=rule.get("severity", "medium"),
                summary=f"Rule '{rule['rule_id']}' matched: {rule.get('description', '')}",
                root_cause=rule.get("root_cause_hint", "See matched rule."),
                matched_rules=[r["rule_id"] for r in matched_rules],
                proposed_fix=rule.get("proposed_fix", ""),
                rollback_reason=rule.get("rollback_reason", ""),
                confidence=0.95,
                flags=[f"rule_matched:{r['rule_id']}" for r in matched_rules],
            )

        # Stage 2: LLM analysis for novel patterns
        if window.error_count == 0 and window.critical_count == 0:
            return MonitoringDecision(
                action="log_only", severity="low",
                summary=f"Window clean: {window.total_count} events, no errors.",
                root_cause="No errors detected.", matched_rules=[], confidence=1.0,
            )

        return self._llm_analyse(window)

    def _match_rules(self, window: LogWindow) -> list[dict]:
        """Match window events against monitoring_rules.yaml patterns."""
        matched = []
        for rule in self._rules:
            if not rule.get("enabled", True):
                continue
            pattern = rule.get("pattern", "")
            min_error_rate = rule.get("min_error_rate", 0.0)
            min_occurrences = rule.get("min_occurrences", 1)
            level_filter = rule.get("level", "").upper()

            # Count pattern matches
            match_count = 0
            for event in window.events:
                if level_filter and event.level.value != level_filter:
                    continue
                if pattern and not event.matches_pattern(pattern):
                    continue
                match_count += 1

            if (match_count >= min_occurrences and
                    window.error_rate >= min_error_rate):
                matched.append({**rule, "_match_count": match_count})

        # Sort by severity (critical > high > medium > low)
        severity_order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        return sorted(matched, key=lambda r: severity_order.get(r.get("severity", "low"), 0),
                      reverse=True)

    def _llm_analyse(self, window: LogWindow) -> MonitoringDecision:
        """Use LLM to classify novel error patterns."""
        context = self.build_context()
        rules_summary = yaml.dump(
            [{"rule_id": r["rule_id"], "pattern": r.get("pattern", ""),
              "description": r.get("description", "")} for r in self._rules],
            default_flow_style=False,
        )

        errors_sample = "\n".join(
            f"[{e.level.value}] {e.service or 'app'}: {e.message[:300]}"
            for e in window.errors_and_above()[:20]
        )

        prompt = f"""
{context}

=== TASK: Runtime Log Analysis ===
You are the LogMonitorAgent. Analyse the following log window and decide
what corrective action the harness should take.

Window summary:
{window.summary()}

Error and critical events (up to 20):
{errors_sample}

Known monitoring rules (already checked — none matched):
{rules_summary}

Decide the appropriate corrective action. Return ONLY valid JSON:
{{
  "action": string,          // one of: log_only, alert_human, open_pr, trigger_rollback
  "severity": string,        // one of: low, medium, high, critical
  "summary": string,         // 1-2 sentence summary for humans
  "root_cause": string,      // best guess at root cause
  "matched_rules": [],       // leave empty — no rules matched (this is LLM analysis)
  "proposed_fix": string,    // if action=open_pr: what the fix should do
  "rollback_reason": string, // if action=trigger_rollback: why rollback is warranted
  "confidence": float,       // 0.0-1.0
  "flags": []                // any additional classification tags
}}

Action selection guidance:
- log_only:         error rate < 5%, no user-facing impact apparent
- alert_human:      error rate 5-20%, or novel pattern needing human judgement
- open_pr:          clear root cause with a specific code/config fix that can be proposed
- trigger_rollback: error rate > 30%, or CRITICAL events indicating severe degradation

Return ONLY valid JSON. No preamble, no markdown fences.
"""
        try:
            response = self._call_llm(prompt)
            parsed = json.loads(response)

            action = parsed.get("action", "alert_human")
            if action not in VALID_ACTIONS:
                action = "alert_human"

            return MonitoringDecision(
                action=action,
                severity=parsed.get("severity", "medium"),
                summary=parsed.get("summary", "LLM analysis complete."),
                root_cause=parsed.get("root_cause", ""),
                matched_rules=parsed.get("matched_rules", []),
                proposed_fix=parsed.get("proposed_fix", ""),
                rollback_reason=parsed.get("rollback_reason", ""),
                confidence=float(parsed.get("confidence", 0.7)),
                flags=parsed.get("flags", []) + ["llm_analysed"],
            )

        except Exception as exc:
            return MonitoringDecision(
                action="alert_human",
                severity="medium",
                summary=f"LLM analysis failed — escalating to human review. Error: {exc}",
                root_cause="LLM call failed during log analysis.",
                matched_rules=[],
                confidence=0.0,
                flags=["llm_analysis_failed"],
            )

    def _load_rules(self) -> list[dict]:
        """Load monitoring_rules.yaml from repo root."""
        rules_path = self.config.repo_root / "monitoring_rules.yaml"
        if not rules_path.exists():
            return []
        try:
            raw = yaml.safe_load(rules_path.read_text()) or {}
            return raw.get("rules", [])
        except Exception:
            return []
