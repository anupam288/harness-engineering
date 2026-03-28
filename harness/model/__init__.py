"""
harness/model/__init__.py — Model factory.

Reads model_config.yaml and returns the correct BaseModel instance
for each agent. Agents never import a provider directly.

Usage:
    from harness.model import build_model
    model = build_model(config, agent_name="requirements_agent")
    response = model.call(prompt)
"""

from __future__ import annotations

from pathlib import Path

import yaml

from harness.model.base_model import BaseModel


def build_model(config, agent_name: str = "default") -> BaseModel:
    """
    Build a model instance for the given agent.

    Resolution order:
      1. agent-specific entry in model_config.yaml
      2. default entry in model_config.yaml
      3. HarnessConfig.llm_model (ultimate fallback)
    """
    model_config_path = config.repo_root / "model_config.yaml"
    if not model_config_path.exists():
        return _build_from_spec(
            {"provider": "anthropic", "model_id": config.llm_model,
             "max_tokens": config.llm_max_tokens}
        )

    raw = yaml.safe_load(model_config_path.read_text()) or {}
    agents = raw.get("agents", {})
    defaults = raw.get("default", {})

    spec = {**defaults, **agents.get(agent_name, {})}
    if not spec:
        spec = {"provider": "anthropic", "model_id": config.llm_model,
                "max_tokens": config.llm_max_tokens}

    # Build fallback if specified
    fallback = None
    if "fallback" in spec:
        fallback = _build_from_spec(spec["fallback"])

    primary = _build_from_spec(spec)
    primary._fallback = fallback  # attach for call_with_fallback()
    return primary


def _build_from_spec(spec: dict) -> BaseModel:
    provider = spec.get("provider", "anthropic").lower()
    model_id = spec.get("model_id", "claude-sonnet-4-20250514")
    max_tokens = spec.get("max_tokens", 2048)
    temperature = spec.get("temperature", 0.0)

    if provider == "anthropic":
        from harness.model.anthropic_model import AnthropicModel
        return AnthropicModel(model_id=model_id, max_tokens=max_tokens, temperature=temperature)

    if provider == "openai":
        from harness.model.openai_model import OpenAIModel
        return OpenAIModel(model_id=model_id, max_tokens=max_tokens, temperature=temperature)

    raise ValueError(
        f"Unknown provider '{provider}'. Supported: anthropic, openai. "
        f"Add a new model file in harness/model/ to support others."
    )
