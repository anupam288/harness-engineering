"""
decision_log.py — Append-only JSONL log of every agent decision.

The decision log is the primary input to the GC agent.
Every AgentResult is appended here, never overwritten.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.agents.base_agent import AgentResult


class DecisionLog:
    def __init__(self, logs_dir: Path):
        self.path = logs_dir / "decision_log.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, result: "AgentResult", signer=None) -> None:
        entry = result.to_dict()
        if signer is not None:
            entry = signer.sign(entry)
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def verify_integrity(self) -> list:
        """
        Verify HMAC signatures on all log entries.
        Returns list of VerificationResult objects.
        Requires HARNESS_LOG_SIGNING_KEY env var to be set.
        """
        from harness.security.log_signer import LogVerifier
        verifier = LogVerifier.from_env()
        if verifier is None:
            return []
        return verifier.verify_log_file(self.path)

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def read_by_phase(self, phase: str) -> list[dict]:
        return [e for e in self.read_all() if e.get("phase") == phase]

    def read_failures(self) -> list[dict]:
        return [e for e in self.read_all() if e.get("status") == "fail"]

    def read_needs_human(self) -> list[dict]:
        return [e for e in self.read_all() if e.get("status") == "needs_human"]
