"""
metrics.py — MetricsCollector

Writes a structured metrics_log.jsonl entry on every agent run.
This is the source of truth for all observability — the dashboard,
aggregator, and budget monitor all read from this file.

Every entry captures:
  - agent identity (name, phase)
  - model used (provider, model_id)
  - token usage (input, output, total)
  - estimated cost (USD, based on pricing in observability_config.yaml)
  - latency (seconds)
  - outcome (status, confidence)
  - review metadata (iterations if self-review was used)
  - run context (timestamp, run_id)

Wired into BaseAgent.execute() — no agent code changes needed.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.agents.base_agent import AgentResult


# Default cost per 1M tokens (USD) — overridden by observability_config.yaml
DEFAULT_PRICING = {
    "claude-sonnet-4-20250514":   {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-opus-4-5":            {"input": 15.00, "output": 75.00},
    "gpt-4o":                     {"input": 2.50,  "output": 10.00},
    "gpt-4o-mini":                {"input": 0.15,  "output": 0.60},
    "default":                    {"input": 3.00,  "output": 15.00},
}


class MetricsEntry:
    """One row in metrics_log.jsonl."""

    def __init__(
        self,
        agent_name: str,
        phase: str,
        model_id: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
        latency_seconds: float,
        status: str,
        confidence: float,
        cost_usd: float,
        run_id: str,
        review_iterations: int = 0,
        flags: list[str] = None,
    ):
        self.agent_name = agent_name
        self.phase = phase
        self.model_id = model_id
        self.provider = provider
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = input_tokens + output_tokens
        self.latency_seconds = latency_seconds
        self.status = status
        self.confidence = confidence
        self.cost_usd = cost_usd
        self.run_id = run_id
        self.review_iterations = review_iterations
        self.flags = flags or []
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "phase": self.phase,
            "model_id": self.model_id,
            "provider": self.provider,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_seconds": self.latency_seconds,
            "status": self.status,
            "confidence": self.confidence,
            "cost_usd": self.cost_usd,
            "review_iterations": self.review_iterations,
            "flags": self.flags,
        }


class MetricsCollector:
    """
    Appends a MetricsEntry to metrics_log.jsonl on every agent run.

    Wired into BaseAgent.execute() — transparent to all agents.
    Also checks token budget thresholds and emits warnings.
    """

    def __init__(self, logs_dir: Path, pricing: dict = None, budgets: dict = None):
        self.logs_dir = logs_dir
        self.metrics_path = logs_dir / "metrics_log.jsonl"
        self.pricing = {**DEFAULT_PRICING, **(pricing or {})}
        self.budgets = budgets or {}  # loaded from observability_config.yaml
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        result: "AgentResult",
        model_id: str = "default",
        provider: str = "anthropic",
        input_tokens: int = 0,
        output_tokens: int = 0,
        latency_seconds: float = 0.0,
        run_id: str = None,
    ) -> MetricsEntry:
        """
        Record metrics for one agent run.
        Called automatically from BaseAgent.execute().
        """
        cost_usd = self._estimate_cost(model_id, input_tokens, output_tokens)
        review_iterations = 0
        if hasattr(result, "review_metadata") and isinstance(result.review_metadata, dict):
            review_iterations = result.review_metadata.get("iterations", 0)

        entry = MetricsEntry(
            agent_name=result.agent_name,
            phase=result.phase,
            model_id=model_id,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_seconds=latency_seconds,
            status=result.status,
            confidence=result.confidence,
            cost_usd=cost_usd,
            run_id=run_id or str(uuid.uuid4())[:8],
            review_iterations=review_iterations,
            flags=list(result.flags),
        )

        self._append(entry)
        self._check_budgets(entry)
        return entry

    def _estimate_cost(self, model_id: str, input_tokens: int, output_tokens: int) -> float:
        prices = self.pricing.get(model_id) or self.pricing.get("default", {"input": 3.0, "output": 15.0})
        input_cost = (input_tokens / 1_000_000) * prices["input"]
        output_cost = (output_tokens / 1_000_000) * prices["output"]
        return round(input_cost + output_cost, 6)

    def _append(self, entry: MetricsEntry) -> None:
        with self.metrics_path.open("a") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

    def _check_budgets(self, entry: MetricsEntry) -> None:
        """Emit warnings when token or cost budgets are approached or exceeded."""
        if not self.budgets:
            return

        # Per-run cost alert
        run_cost_limit = self.budgets.get("alert_per_run_cost_usd")
        if run_cost_limit and entry.cost_usd >= run_cost_limit:
            print(
                f"  ⚠ BUDGET ALERT: {entry.agent_name} run cost "
                f"${entry.cost_usd:.4f} ≥ threshold ${run_cost_limit:.4f}"
            )

        # Per-run token alert
        run_token_limit = self.budgets.get("alert_per_run_tokens")
        if run_token_limit and entry.total_tokens >= run_token_limit:
            print(
                f"  ⚠ BUDGET ALERT: {entry.agent_name} used "
                f"{entry.total_tokens:,} tokens ≥ threshold {run_token_limit:,}"
            )

    def read_all(self) -> list[dict]:
        if not self.metrics_path.exists():
            return []
        lines = self.metrics_path.read_text().splitlines()
        return [json.loads(line) for line in lines if line.strip()]
