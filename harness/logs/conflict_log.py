"""
conflict_log.py — Records disagreements between parallel agents.
override_log.py  — Records human overrides of agent decisions.

Both are append-only JSONL. Both are primary inputs to the GC agent.
A conflict = two agents produced contradictory outputs on the same input.
An override = a human manually changed what an agent decided.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class ConflictLog:
    """
    Records when two or more parallel agents disagree.

    Example: Bureau agent scores LOW risk, Fraud agent scores HIGH risk.
    The Orchestrator resolves it via conflict_policy.yaml, but the
    disagreement is recorded here so the GC agent can spot patterns.
    """

    def __init__(self, logs_dir: Path):
        self.path = logs_dir / "conflict_log.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        input_id: str,
        agent_a: str,
        output_a: dict,
        agent_b: str,
        output_b: dict,
        resolution: str,
        resolved_by: str = "conflict_policy.yaml",
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "input_id": input_id,
            "agent_a": agent_a,
            "output_a": output_a,
            "agent_b": agent_b,
            "output_b": output_b,
            "resolution": resolution,
            "resolved_by": resolved_by,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def most_frequent_pairs(self, top_n: int = 5) -> list[tuple[str, str, int]]:
        """Return the agent pairs that conflict most often."""
        from collections import Counter
        pairs = Counter(
            tuple(sorted([e["agent_a"], e["agent_b"]]))
            for e in self.read_all()
        )
        return [(a, b, count) for (a, b), count in pairs.most_common(top_n)]


class OverrideLog:
    """
    Records every human override of an agent decision.

    These are the most valuable GC agent inputs — each override
    is evidence that the harness missed a rule that should be
    encoded in policy files.
    """

    def __init__(self, logs_dir: Path):
        self.path = logs_dir / "override_log.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        input_id: str,
        agent_name: str,
        agent_decision: dict,
        human_decision: dict,
        reason: str,
        overrider: str = "human",
    ) -> None:
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "input_id": input_id,
            "agent_name": agent_name,
            "agent_decision": agent_decision,
            "human_decision": human_decision,
            "reason": reason,
            "overrider": overrider,
        }
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def read_by_agent(self, agent_name: str) -> list[dict]:
        return [e for e in self.read_all() if e.get("agent_name") == agent_name]
