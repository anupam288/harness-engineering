"""
base_model.py — Abstract interface all model implementations must satisfy.

Every provider (Anthropic, OpenAI, local) implements BaseModel.
Agents never import a provider directly — they call self.model.call().
Swapping providers = changing model_config.yaml, not touching agent code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class ModelResponse:
    """Normalised response returned by every provider."""
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_seconds: float = 0.0
    provider: str = "unknown"

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "model": self.model,
            "provider": self.provider,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_seconds": self.latency_seconds,
        }


class BaseModel(ABC):
    """
    Abstract provider interface.

    Subclasses implement:
      call(prompt, system, max_tokens, temperature) → ModelResponse
      stream(prompt, system, max_tokens) → Iterator[str]

    The base class provides:
      call_with_retry() — automatic retry with exponential backoff
      call_with_fallback() — falls back to a cheaper model on failure
    """

    def __init__(self, model_id: str, max_tokens: int = 2048, temperature: float = 0.0):
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.temperature = temperature

    @abstractmethod
    def call(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = None,
        temperature: float = None,
    ) -> ModelResponse:
        """Single synchronous call. Must return a ModelResponse."""
        ...

    @abstractmethod
    def stream(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = None,
    ) -> Iterator[str]:
        """Streaming call. Yields text chunks."""
        ...

    def call_with_retry(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = None,
        retries: int = 3,
        backoff_seconds: float = 2.0,
    ) -> ModelResponse:
        """
        Retry on transient errors with exponential backoff.
        Raises the last exception if all retries fail.
        """
        import time

        last_exc = None
        for attempt in range(retries):
            try:
                return self.call(prompt, system, max_tokens)
            except Exception as exc:
                last_exc = exc
                if attempt < retries - 1:
                    wait = backoff_seconds * (2 ** attempt)
                    time.sleep(wait)
        raise last_exc

    def call_with_fallback(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = None,
        fallback: "BaseModel" = None,
    ) -> ModelResponse:
        """
        Try primary model; on failure use fallback model.
        Records which model actually served the response.
        """
        try:
            return self.call_with_retry(prompt, system, max_tokens)
        except Exception as primary_exc:
            if fallback is None:
                raise primary_exc
            response = fallback.call_with_retry(prompt, system, max_tokens)
            response.text = f"[FALLBACK from {self.model_id}] " + response.text
            return response
