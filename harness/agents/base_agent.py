"""
base_agent.py — Base class for all SDLC harness agents.

Every agent in the harness inherits from BaseAgent. This enforces:
- Agents.md is always injected into context
- All policy files are loaded before any LLM call
- Outputs are always logged to decision_log
- Agents never write to policy files at runtime
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from harness.config import HarnessConfig
from harness.logs.decision_log import DecisionLog


class AgentResult:
    """Structured output every agent must return."""

    def __init__(
        self,
        agent_name: str,
        phase: str,
        status: str,           # "pass" | "fail" | "needs_human"
        output: dict,
        confidence: float,     # 0.0 – 1.0
        artifacts_produced: list[str] = None,
        flags: list[str] = None,
    ):
        self.agent_name = agent_name
        self.phase = phase
        self.status = status
        self.output = output
        self.confidence = confidence
        self.artifacts_produced = artifacts_produced or []
        self.flags = flags or []
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "phase": self.phase,
            "status": self.status,
            "output": self.output,
            "confidence": self.confidence,
            "artifacts_produced": self.artifacts_produced,
            "flags": self.flags,
            "timestamp": self.timestamp,
        }

    def passed(self) -> bool:
        return self.status == "pass"

    def needs_human(self) -> bool:
        return self.status == "needs_human"


class BaseAgent(ABC):
    """
    All harness agents inherit from this class.

    Subclasses implement:
      - run(input_data) → AgentResult

    The base class handles:
      - Loading AGENTS.md and policy files into context
      - Enforcing read-only policy file access
      - Logging every result to decision_log
      - Measuring latency
    """

    POLICY_FILES_READONLY = True  # Agents may never write policy files

    def __init__(self, config: HarnessConfig):
        self.config = config
        self.name = self.__class__.__name__
        self.phase = "unknown"
        self._decision_log = DecisionLog(config.logs_dir)
        self._agents_md = self._load_agents_md()
        self._policies = self._load_policies()

    # ------------------------------------------------------------------
    # Context loading (harness core: everything the agent can see)
    # ------------------------------------------------------------------

    def _load_agents_md(self) -> str:
        agents_md_path = self.config.repo_root / "AGENTS.md"
        if agents_md_path.exists():
            return agents_md_path.read_text()
        return "# AGENTS.md not found — create it before running agents."

    def _load_policies(self) -> dict:
        """Load all policy files. Agents may read but never write these."""
        policies = {}
        policies_dir = self.config.repo_root / "policies"
        if not policies_dir.exists():
            return policies

        for policy_file in policies_dir.iterdir():
            if policy_file.suffix in (".yaml", ".yml"):
                import yaml
                policies[policy_file.stem] = yaml.safe_load(policy_file.read_text()) or {}
            elif policy_file.suffix == ".json":
                policies[policy_file.stem] = json.loads(policy_file.read_text())

        return policies

    def _load_doc(self, doc_name: str) -> str:
        """Load a versioned doc from docs/ into agent context."""
        doc_path = self.config.repo_root / "docs" / doc_name
        if doc_path.exists():
            return doc_path.read_text()
        return f"# {doc_name} not yet created."

    def build_context(self, extra_docs: list[str] = None) -> str:
        """
        Assemble the full context string injected into the agent's LLM call.
        AGENTS.md + relevant policy files + requested docs.
        """
        parts = [
            "=== AGENTS.MD (master map) ===",
            self._agents_md,
            "",
            "=== POLICY FILES (read-only) ===",
        ]

        for name, content in self._policies.items():
            parts.append(f"--- {name} ---")
            parts.append(json.dumps(content, indent=2))

        if extra_docs:
            parts.append("")
            parts.append("=== REFERENCE DOCS ===")
            for doc_name in extra_docs:
                parts.append(f"--- {doc_name} ---")
                parts.append(self._load_doc(doc_name))

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Write helpers (for docs/ only — never policies/)
    # ------------------------------------------------------------------

    def write_artifact(self, filename: str, content: str) -> Path:
        """
        Write a harness artifact to docs/.
        Agents may write docs but never policy files.
        """
        target = self.config.repo_root / "docs" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return target

    def append_to_artifact(self, filename: str, content: str) -> Path:
        target = self.config.repo_root / "docs" / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as f:
            f.write("\n" + content)
        return target

    # ------------------------------------------------------------------
    # Run wrapper — timing + logging
    # ------------------------------------------------------------------

    def execute(self, input_data: dict) -> AgentResult:
        """Public entrypoint. Wraps run() with logging and timing."""
        start = time.monotonic()
        try:
            result = self.run(input_data)
        except Exception as exc:
            result = AgentResult(
                agent_name=self.name,
                phase=self.phase,
                status="fail",
                output={"error": str(exc)},
                confidence=0.0,
                flags=[f"unhandled_exception: {type(exc).__name__}"],
            )

        elapsed = time.monotonic() - start
        result.output["latency_seconds"] = round(elapsed, 3)

        # Always log — harness rule
        self._decision_log.append(result)

        return result

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    @abstractmethod
    def run(self, input_data: dict) -> AgentResult:
        """
        Implement the agent's core logic here.
        Return an AgentResult — never raise from here.
        """
        ...
