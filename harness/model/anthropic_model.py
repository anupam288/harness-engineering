"""
anthropic_model.py — Anthropic Claude provider.

Implements BaseModel using the Anthropic SDK.
All existing agents that previously called Anthropic directly
are re-routed through this class via HarnessConfig.
"""

from __future__ import annotations

import time
from typing import Iterator

from harness.model.base_model import BaseModel, ModelResponse


class AnthropicModel(BaseModel):
    """
    Anthropic Claude via the official SDK.

    Supported model IDs:
      claude-sonnet-4-20250514   (default — smart + fast)
      claude-opus-4-5            (most capable, slower)
      claude-haiku-4-5-20251001  (fastest, cheapest — good for linting tasks)
    """

    PROVIDER = "anthropic"

    def __init__(self, model_id: str = "claude-sonnet-4-20250514", max_tokens: int = 2048, temperature: float = 0.0):
        super().__init__(model_id, max_tokens, temperature)
        import anthropic
        self._client = anthropic.Anthropic()

    def call(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = None,
        temperature: float = None,
    ) -> ModelResponse:
        start = time.monotonic()

        messages = [{"role": "user", "content": prompt}]
        kwargs = dict(
            model=self.model_id,
            max_tokens=max_tokens or self.max_tokens,
            messages=messages,
        )
        if system:
            kwargs["system"] = system

        response = self._client.messages.create(**kwargs)

        return ModelResponse(
            text=response.content[0].text,
            model=self.model_id,
            provider=self.PROVIDER,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            latency_seconds=round(time.monotonic() - start, 3),
        )

    def stream(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = None,
    ) -> Iterator[str]:
        messages = [{"role": "user", "content": prompt}]
        kwargs = dict(
            model=self.model_id,
            max_tokens=max_tokens or self.max_tokens,
            messages=messages,
        )
        if system:
            kwargs["system"] = system

        with self._client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                yield text
