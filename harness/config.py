"""
config.py — Central configuration for the SDLC harness.

Loads harness settings from harness_config.yaml at repo root.
All agents receive a HarnessConfig instance — never raw paths.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class HarnessConfig:
    repo_root: Path
    logs_dir: Path
    docs_dir: Path
    policies_dir: Path
    llm_model: str = "claude-sonnet-4-20250514"
    llm_max_tokens: int = 2048
    confidence_threshold: float = 0.75   # below this → needs_human
    canary_quality_threshold: float = 0.85
    gc_agent_schedule: str = "0 2 * * *"  # 2am nightly cron
    phase_gates_strict: bool = True       # if True, blocks on gate fail

    # Per-phase gate document requirements
    phase_gates: dict = field(default_factory=lambda: {
        "requirements": {
            "required_docs": ["requirements.md"],
            "required_policies": [],
            "zero_open_items_in": [],
        },
        "design": {
            "required_docs": ["requirements.md", "architecture.md"],
            "required_policies": ["policy", "conflict_policy"],
            "zero_open_items_in": ["uncertain_terms.md"],
        },
        "development": {
            "required_docs": ["architecture.md"],
            "required_policies": ["policy", "conflict_policy"],
            "zero_open_items_in": [],
        },
        "testing": {
            "required_docs": ["architecture.md", "requirements.md"],
            "required_policies": ["policy"],
            "zero_open_items_in": [],
        },
        "deployment": {
            "required_docs": ["edge_cases.md"],
            "required_policies": ["rollback_triggers"],
            "zero_open_items_in": ["edge_cases.md"],
        },
        "monitoring": {
            "required_docs": [],
            "required_policies": ["rollback_triggers"],
            "zero_open_items_in": [],
        },
    })

    @classmethod
    def from_repo(cls, repo_root: Path | str = None) -> "HarnessConfig":
        """
        Load config from harness_config.yaml at repo root.
        Falls back to sensible defaults if file doesn't exist.
        """
        repo_root = Path(repo_root or os.getcwd())
        config_path = repo_root / "harness_config.yaml"

        overrides = {}
        if config_path.exists():
            overrides = yaml.safe_load(config_path.read_text()) or {}

        logs_dir = repo_root / overrides.pop("logs_dir", ".harness/logs")
        docs_dir = repo_root / overrides.pop("docs_dir", "docs")
        policies_dir = repo_root / overrides.pop("policies_dir", "policies")

        logs_dir.mkdir(parents=True, exist_ok=True)
        docs_dir.mkdir(parents=True, exist_ok=True)
        policies_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            repo_root=repo_root,
            logs_dir=logs_dir,
            docs_dir=docs_dir,
            policies_dir=policies_dir,
            **overrides,
        )

    def summary(self) -> str:
        return (
            f"HarnessConfig\n"
            f"  repo_root:   {self.repo_root}\n"
            f"  llm_model:   {self.llm_model}\n"
            f"  confidence ≥ {self.confidence_threshold}\n"
            f"  strict gates: {self.phase_gates_strict}\n"
        )
