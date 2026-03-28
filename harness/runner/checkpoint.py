"""
checkpoint.py — Agent-level checkpointing

Saves intermediate AgentResult objects so a failed pipeline can
resume from the last successful agent, not from scratch.

Two levels of checkpointing:
  1. Phase-level  — PipelineCheckpoint in pipeline.py (which phases are done)
  2. Agent-level  — AgentCheckpoint here (which agents within a phase are done)

Agent checkpoints are keyed by: (phase, agent_name, input_hash)
The input_hash ensures that if the input changes, the checkpoint is invalidated.

Usage:
    checkpoint = AgentCheckpoint(config)

    # Before running an agent:
    cached = checkpoint.load("requirements", "RequirementsAgent", input_data)
    if cached:
        return cached   # skip the agent entirely

    # After a successful run:
    result = agent.execute(input_data)
    if result.passed():
        checkpoint.save("requirements", "RequirementsAgent", input_data, result)
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.agents.base_agent import AgentResult
    from harness.config import HarnessConfig


class AgentCheckpoint:
    """
    Saves and restores AgentResult objects between runs.

    Checkpoints are stored as JSON files in .harness/checkpoints/.
    Each file is keyed by phase + agent name + input hash.
    """

    def __init__(self, config: "HarnessConfig"):
        self._dir = config.logs_dir.parent / "checkpoints"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ttl_hours = 24   # checkpoints expire after 24 hours

    def save(
        self,
        phase: str,
        agent_name: str,
        input_data: dict,
        result: "AgentResult",
    ) -> Path:
        """Persist a successful AgentResult. Only saves pass/needs_human, never fail."""
        if result.status == "fail":
            return None   # never checkpoint failures

        key = self._key(phase, agent_name, input_data)
        checkpoint_path = self._dir / f"{key}.json"
        checkpoint_path.write_text(json.dumps({
            "phase": phase,
            "agent_name": agent_name,
            "input_hash": self._input_hash(input_data),
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "result": result.to_dict(),
        }, indent=2))
        return checkpoint_path

    def load(
        self,
        phase: str,
        agent_name: str,
        input_data: dict,
    ) -> "AgentResult | None":
        """
        Load a cached AgentResult if one exists and is not expired.
        Returns None if no valid checkpoint found.
        """
        key = self._key(phase, agent_name, input_data)
        checkpoint_path = self._dir / f"{key}.json"

        if not checkpoint_path.exists():
            return None

        try:
            data = json.loads(checkpoint_path.read_text())
        except Exception:
            return None

        # Check TTL
        saved_at = datetime.fromisoformat(data["saved_at"])
        age_hours = (datetime.now(timezone.utc) - saved_at).total_seconds() / 3600
        if age_hours > self._ttl_hours:
            checkpoint_path.unlink(missing_ok=True)
            return None

        # Reconstruct AgentResult
        return self._deserialise(data["result"])

    def clear(self, phase: str = None, agent_name: str = None) -> int:
        """
        Clear checkpoints. Returns number of files deleted.
        If both phase and agent_name are None, clears all checkpoints.
        """
        deleted = 0
        for f in self._dir.glob("*.json"):
            if phase and not f.name.startswith(f"{phase}_"):
                continue
            if agent_name and agent_name.lower() not in f.name:
                continue
            f.unlink()
            deleted += 1
        return deleted

    def list_all(self) -> list[dict]:
        """Return metadata for all stored checkpoints."""
        checkpoints = []
        for f in sorted(self._dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                checkpoints.append({
                    "phase": data.get("phase"),
                    "agent_name": data.get("agent_name"),
                    "saved_at": data.get("saved_at"),
                    "status": data.get("result", {}).get("status"),
                    "file": f.name,
                })
            except Exception:
                continue
        return checkpoints

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _key(self, phase: str, agent_name: str, input_data: dict) -> str:
        input_hash = self._input_hash(input_data)
        return f"{phase}_{agent_name.lower()}_{input_hash}"

    @staticmethod
    def _input_hash(input_data: dict) -> str:
        """Stable hash of input_data for cache invalidation."""
        try:
            serialised = json.dumps(input_data, sort_keys=True, default=str)
        except Exception:
            serialised = str(input_data)
        return hashlib.md5(serialised.encode()).hexdigest()[:8]

    @staticmethod
    def _deserialise(result_dict: dict) -> "AgentResult":
        from harness.agents.base_agent import AgentResult
        r = AgentResult(
            agent_name=result_dict["agent_name"],
            phase=result_dict["phase"],
            status=result_dict["status"],
            output=result_dict.get("output", {}),
            confidence=result_dict.get("confidence", 0.0),
            artifacts_produced=result_dict.get("artifacts_produced", []),
            flags=result_dict.get("flags", []),
        )
        r.review_metadata = result_dict.get("review_metadata", {})
        return r
