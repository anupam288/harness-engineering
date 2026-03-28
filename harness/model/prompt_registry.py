"""
prompt_registry.py — Versioned prompt loader.

Prompts live in prompts/ as markdown files, versioned in the repo.
The GC agent can detect when a prompt change correlates with a
confidence drop by diffing prompt versions in the decision log.

Usage:
    registry = PromptRegistry(repo_root)
    prompt = registry.get("requirements_agent", variables={"domain": "lending"})

Prompt files support simple {{variable}} interpolation.
"""

from __future__ import annotations

import re
from pathlib import Path


class PromptRegistry:
    """
    Loads prompt templates from prompts/ directory.

    File naming convention:
      prompts/<agent_name>.md         — primary prompt
      prompts/<agent_name>.system.md  — system prompt (optional)

    Variable interpolation:
      Write {{variable_name}} in the prompt file.
      Pass variables={"variable_name": "value"} to get().
    """

    VARIABLE_PATTERN = re.compile(r"\{\{(\w+)\}\}")

    def __init__(self, repo_root: Path):
        self.prompts_dir = repo_root / "prompts"
        self._cache: dict[str, str] = {}

    def get(self, agent_name: str, variables: dict = None) -> str:
        """
        Load and interpolate the prompt for an agent.
        Returns empty string if no prompt file exists (agent uses inline prompt).
        """
        prompt = self._load(agent_name)
        if not prompt:
            return ""
        return self._interpolate(prompt, variables or {})

    def get_system(self, agent_name: str, variables: dict = None) -> str:
        """Load the system prompt for an agent (optional)."""
        prompt = self._load(f"{agent_name}.system")
        if not prompt:
            return ""
        return self._interpolate(prompt, variables or {})

    def exists(self, agent_name: str) -> bool:
        return (self.prompts_dir / f"{agent_name}.md").exists()

    def list_all(self) -> list[str]:
        """Return all registered agent prompt names."""
        if not self.prompts_dir.exists():
            return []
        return [
            p.stem for p in self.prompts_dir.glob("*.md")
            if not p.stem.endswith(".system")
        ]

    def _load(self, name: str) -> str:
        if name in self._cache:
            return self._cache[name]
        path = self.prompts_dir / f"{name}.md"
        if not path.exists():
            return ""
        content = path.read_text()
        self._cache[name] = content
        return content

    def _interpolate(self, template: str, variables: dict) -> str:
        """Replace {{variable}} placeholders with values."""
        def replacer(match):
            key = match.group(1)
            if key not in variables:
                return match.group(0)  # leave unreplaced if variable missing
            return str(variables[key])
        return self.VARIABLE_PATTERN.sub(replacer, template)

    def validate_variables(self, agent_name: str, variables: dict) -> list[str]:
        """
        Check that all {{variables}} in a prompt are provided.
        Returns list of missing variable names.
        """
        template = self._load(agent_name)
        if not template:
            return []
        required = set(self.VARIABLE_PATTERN.findall(template))
        provided = set(variables.keys())
        return sorted(required - provided)
