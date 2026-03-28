"""
openai_model.py — OpenAI provider (drop-in swap for AnthropicModel).

Set provider: openai in model_config.yaml to use this instead.
Requires: pip install openai
"""

from __future__ import annotations

import time
from typing import Iterator

from harness.model.base_model import BaseModel, ModelResponse


class OpenAIModel(BaseModel):
    """
    OpenAI GPT via the official SDK.

    Supported model IDs:
      gpt-4o          (default)
      gpt-4o-mini     (faster, cheaper)
      o3-mini         (reasoning tasks)
    """

    PROVIDER = "openai"

    def __init__(self, model_id: str = "gpt-4o", max_tokens: int = 2048, temperature: float = 0.0):
        super().__init__(model_id, max_tokens, temperature)
        try:
            import openai
            self._client = openai.OpenAI()
        except ImportError:
            raise ImportError(
                "openai package not installed. Run: pip install openai"
            )

    def call(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = None,
        temperature: float = None,
    ) -> ModelResponse:
        start = time.monotonic()

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = self._client.chat.completions.create(
            model=self.model_id,
            max_tokens=max_tokens or self.max_tokens,
            temperature=temperature if temperature is not None else self.temperature,
            messages=messages,
        )

        return ModelResponse(
            text=response.choices[0].message.content,
            model=self.model_id,
            provider=self.PROVIDER,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            latency_seconds=round(time.monotonic() - start, 3),
        )

    def stream(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = None,
    ) -> Iterator[str]:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        stream = self._client.chat.completions.create(
            model=self.model_id,
            max_tokens=max_tokens or self.max_tokens,
            messages=messages,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
