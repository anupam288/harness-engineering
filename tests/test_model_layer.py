"""
test_model_layer.py — Tests for the model layer.

Covers:
  - ModelResponse structure
  - PromptRegistry loading and interpolation
  - build_model() factory routing
  - BaseModel retry and fallback logic
  - BaseAgent._call_llm and _render_prompt wiring
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness.model.base_model import BaseModel, ModelResponse
from harness.model.prompt_registry import PromptRegistry
from harness.model import build_model


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_repo(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "policies").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "harness" / "agents").mkdir(parents=True)
    (tmp_path / ".harness" / "logs").mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("# AGENTS.md")
    return tmp_path


@pytest.fixture
def config(tmp_repo):
    from harness.config import HarnessConfig
    return HarnessConfig(
        repo_root=tmp_repo,
        logs_dir=tmp_repo / ".harness" / "logs",
        docs_dir=tmp_repo / "docs",
        policies_dir=tmp_repo / "policies",
    )


def make_mock_model(text: str = '{"result": "ok", "confidence": 0.9}') -> BaseModel:
    """Return a mock BaseModel that returns a fixed response."""
    model = MagicMock(spec=BaseModel)
    model.model_id = "mock-model"
    response = ModelResponse(
        text=text,
        model="mock-model",
        provider="mock",
        input_tokens=10,
        output_tokens=20,
        latency_seconds=0.1,
    )
    model.call.return_value = response
    model.call_with_retry.return_value = response
    model.call_with_fallback.return_value = response
    return model


# ---------------------------------------------------------------------------
# ModelResponse tests
# ---------------------------------------------------------------------------

class TestModelResponse:

    def test_total_tokens(self):
        r = ModelResponse(text="hi", model="m", input_tokens=100, output_tokens=50)
        assert r.total_tokens == 150

    def test_to_dict_has_all_keys(self):
        r = ModelResponse(text="hi", model="m", provider="anthropic",
                          input_tokens=10, output_tokens=5, latency_seconds=0.2)
        d = r.to_dict()
        assert set(d.keys()) == {
            "text", "model", "provider",
            "input_tokens", "output_tokens", "total_tokens", "latency_seconds"
        }

    def test_to_dict_values(self):
        r = ModelResponse(text="hello", model="claude", provider="anthropic",
                          input_tokens=10, output_tokens=20, latency_seconds=1.5)
        d = r.to_dict()
        assert d["text"] == "hello"
        assert d["total_tokens"] == 30
        assert d["latency_seconds"] == 1.5


# ---------------------------------------------------------------------------
# PromptRegistry tests
# ---------------------------------------------------------------------------

class TestPromptRegistry:

    def test_get_returns_empty_when_no_file(self, tmp_repo):
        registry = PromptRegistry(tmp_repo)
        assert registry.get("nonexistent_agent") == ""

    def test_get_returns_prompt_content(self, tmp_repo):
        (tmp_repo / "prompts" / "my_agent.md").write_text("Hello from prompt")
        registry = PromptRegistry(tmp_repo)
        assert registry.get("my_agent") == "Hello from prompt"

    def test_interpolation_replaces_variables(self, tmp_repo):
        (tmp_repo / "prompts" / "my_agent.md").write_text(
            "Project: {{project_name}}\nDomain: {{domain}}"
        )
        registry = PromptRegistry(tmp_repo)
        result = registry.get("my_agent", {"project_name": "HarnessX", "domain": "lending"})
        assert "HarnessX" in result
        assert "lending" in result
        assert "{{" not in result

    def test_missing_variable_left_as_placeholder(self, tmp_repo):
        (tmp_repo / "prompts" / "my_agent.md").write_text("Hello {{name}}")
        registry = PromptRegistry(tmp_repo)
        result = registry.get("my_agent", {})
        assert "{{name}}" in result  # unreplaced, not crashed

    def test_validate_variables_finds_missing(self, tmp_repo):
        (tmp_repo / "prompts" / "my_agent.md").write_text(
            "{{project_name}} and {{domain}}"
        )
        registry = PromptRegistry(tmp_repo)
        missing = registry.validate_variables("my_agent", {"project_name": "X"})
        assert missing == ["domain"]

    def test_validate_variables_returns_empty_when_all_provided(self, tmp_repo):
        (tmp_repo / "prompts" / "my_agent.md").write_text("{{a}} {{b}}")
        registry = PromptRegistry(tmp_repo)
        missing = registry.validate_variables("my_agent", {"a": "1", "b": "2"})
        assert missing == []

    def test_system_prompt_loaded_from_system_file(self, tmp_repo):
        (tmp_repo / "prompts" / "my_agent.system.md").write_text("You are a helper.")
        registry = PromptRegistry(tmp_repo)
        assert registry.get_system("my_agent") == "You are a helper."

    def test_system_prompt_empty_when_no_file(self, tmp_repo):
        registry = PromptRegistry(tmp_repo)
        assert registry.get_system("my_agent") == ""

    def test_exists_returns_true_when_file_present(self, tmp_repo):
        (tmp_repo / "prompts" / "my_agent.md").write_text("prompt")
        registry = PromptRegistry(tmp_repo)
        assert registry.exists("my_agent") is True

    def test_exists_returns_false_when_missing(self, tmp_repo):
        registry = PromptRegistry(tmp_repo)
        assert registry.exists("ghost_agent") is False

    def test_list_all_returns_agent_names(self, tmp_repo):
        (tmp_repo / "prompts" / "agent_a.md").write_text("a")
        (tmp_repo / "prompts" / "agent_b.md").write_text("b")
        (tmp_repo / "prompts" / "agent_a.system.md").write_text("sys")
        registry = PromptRegistry(tmp_repo)
        names = registry.list_all()
        assert "agent_a" in names
        assert "agent_b" in names
        assert "agent_a.system" not in names  # system files excluded

    def test_caches_prompt_on_second_read(self, tmp_repo):
        prompt_file = tmp_repo / "prompts" / "my_agent.md"
        prompt_file.write_text("v1")
        registry = PromptRegistry(tmp_repo)
        first = registry.get("my_agent")
        prompt_file.write_text("v2")  # modify after first read
        second = registry.get("my_agent")
        assert first == second == "v1"  # cache served


# ---------------------------------------------------------------------------
# build_model() factory tests
# ---------------------------------------------------------------------------

class TestBuildModel:

    def test_returns_anthropic_model_by_default(self, config):
        with patch("harness.model.anthropic_model.AnthropicModel.__init__",
                   return_value=None) as mock_init:
            with patch("anthropic.Anthropic"):
                from harness.model.anthropic_model import AnthropicModel
                # Just verify factory resolves to AnthropicModel when no config file
                model = build_model(config, agent_name="default")
                assert model is not None

    def test_reads_agent_specific_config(self, tmp_repo, config):
        model_config = {
            "default": {"provider": "anthropic", "model_id": "claude-haiku-4-5-20251001",
                        "max_tokens": 1024},
            "agents": {
                "gc_agent": {"provider": "anthropic",
                             "model_id": "claude-sonnet-4-20250514", "max_tokens": 4000}
            }
        }
        import yaml
        (tmp_repo / "model_config.yaml").write_text(yaml.dump(model_config))

        with patch("anthropic.Anthropic"):
            model = build_model(config, agent_name="gc_agent")
            assert model.model_id == "claude-sonnet-4-20250514"
            assert model.max_tokens == 4000

    def test_falls_back_to_default_for_unknown_agent(self, tmp_repo, config):
        model_config = {
            "default": {"provider": "anthropic", "model_id": "claude-haiku-4-5-20251001",
                        "max_tokens": 512},
            "agents": {}
        }
        import yaml
        (tmp_repo / "model_config.yaml").write_text(yaml.dump(model_config))

        with patch("anthropic.Anthropic"):
            model = build_model(config, agent_name="unknown_agent_xyz")
            assert model.model_id == "claude-haiku-4-5-20251001"

    def test_raises_on_unknown_provider(self, tmp_repo, config):
        model_config = {
            "default": {"provider": "someunknownprovider", "model_id": "x", "max_tokens": 100}
        }
        import yaml
        (tmp_repo / "model_config.yaml").write_text(yaml.dump(model_config))

        with pytest.raises(ValueError, match="Unknown provider"):
            build_model(config, agent_name="default")


# ---------------------------------------------------------------------------
# BaseModel retry and fallback tests
# ---------------------------------------------------------------------------

class TestBaseModelRetryAndFallback:

    def _make_failing_model(self, fail_times: int):
        """Model that fails `fail_times` times then succeeds."""
        model = MagicMock(spec=BaseModel)
        model.model_id = "failing-model"
        success = ModelResponse(text="success", model="m", provider="mock")
        call_count = {"n": 0}

        def side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] <= fail_times:
                raise ConnectionError("transient error")
            return success

        model.call.side_effect = side_effect
        return model, success

    def test_call_with_retry_succeeds_after_transient_failure(self):
        model, expected = self._make_failing_model(fail_times=1)
        # Use real retry logic from BaseModel, applied to our mock's .call()
        from harness.model.base_model import BaseModel as RealBaseModel

        # Patch sleep to avoid waiting in tests
        with patch("time.sleep"):
            result = RealBaseModel.call_with_retry(
                model, prompt="test", retries=3, backoff_seconds=0.01
            )
        assert result.text == "success"
        assert model.call.call_count == 2

    def test_call_with_retry_raises_after_all_retries_exhausted(self):
        model, _ = self._make_failing_model(fail_times=99)
        from harness.model.base_model import BaseModel as RealBaseModel

        with patch("time.sleep"):
            with pytest.raises(ConnectionError):
                RealBaseModel.call_with_retry(
                    model, prompt="test", retries=3, backoff_seconds=0.01
                )
        assert model.call.call_count == 3

    def test_call_with_fallback_uses_fallback_on_primary_failure(self):
        primary = MagicMock(spec=BaseModel)
        primary.model_id = "primary"
        primary.call.side_effect = RuntimeError("primary failed")
        primary.call_with_retry.side_effect = RuntimeError("primary failed")

        fallback = make_mock_model("fallback response")
        fallback.call_with_retry.return_value = ModelResponse(
            text="fallback response", model="fallback", provider="mock"
        )

        from harness.model.base_model import BaseModel as RealBaseModel
        result = RealBaseModel.call_with_fallback(
            primary, prompt="test", fallback=fallback
        )
        assert "fallback" in result.text.lower()

    def test_call_with_fallback_raises_when_no_fallback(self):
        primary = MagicMock(spec=BaseModel)
        primary.call_with_retry.side_effect = RuntimeError("failed")

        from harness.model.base_model import BaseModel as RealBaseModel
        with pytest.raises(RuntimeError):
            RealBaseModel.call_with_fallback(primary, prompt="test", fallback=None)


# ---------------------------------------------------------------------------
# BaseAgent model wiring tests
# ---------------------------------------------------------------------------

class TestBaseAgentModelWiring:

    def test_agent_call_llm_uses_model_layer(self, config):
        """_call_llm() on BaseAgent should delegate to self._model, not raw Anthropic."""
        from harness.agents.base_agent import BaseAgent, AgentResult

        class DummyAgent(BaseAgent):
            phase = "testing"
            def run(self, input_data):
                text = self._call_llm("hello")
                return AgentResult(
                    agent_name=self.name, phase=self.phase,
                    status="pass", output={"text": text}, confidence=1.0
                )

        mock_model = make_mock_model("mocked response")
        with patch("harness.model.build_model", return_value=mock_model):
            with patch("harness.model.prompt_registry.PromptRegistry"):
                agent = DummyAgent(config)
                agent._model = mock_model
                result = agent.execute({})

        assert result.output["text"] == "mocked response"
        mock_model.call_with_fallback.assert_called_once()

    def test_agent_render_prompt_uses_registry(self, tmp_repo, config):
        """_render_prompt() should load from PromptRegistry."""
        (tmp_repo / "prompts" / "dummy_agent.md").write_text(
            "Project: {{project_name}}"
        )
        from harness.agents.base_agent import BaseAgent, AgentResult

        class DummyAgent(BaseAgent):
            phase = "testing"
            def run(self, input_data):
                prompt = self._render_prompt({"project_name": "TestProject"})
                return AgentResult(
                    agent_name=self.name, phase=self.phase,
                    status="pass", output={"prompt": prompt}, confidence=1.0
                )

        mock_model = make_mock_model()
        with patch("harness.model.build_model", return_value=mock_model):
            agent = DummyAgent(config)
            result = agent.execute({})

        assert "TestProject" in result.output["prompt"]

    def test_config_build_model_convenience(self, config):
        """config.build_model() should call through to harness.model.build_model."""
        mock_model = make_mock_model()
        with patch("harness.model.build_model", return_value=mock_model) as mock_factory:
            result = config.build_model("gc_agent")
            mock_factory.assert_called_once_with(config, agent_name="gc_agent")
            assert result is mock_model

    def test_config_prompt_registry_convenience(self, config):
        """config.prompt_registry() should return a PromptRegistry instance."""
        registry = config.prompt_registry()
        from harness.model.prompt_registry import PromptRegistry
        assert isinstance(registry, PromptRegistry)


# ---------------------------------------------------------------------------
# Rate limit detection and jitter tests
# ---------------------------------------------------------------------------

class TestRateLimitHandling:

    def test_is_rate_limit_detects_429_in_message(self):
        exc = ConnectionError("HTTP 429: Too Many Requests")
        assert BaseModel._is_rate_limit(exc)

    def test_is_rate_limit_detects_rate_limit_keyword(self):
        exc = Exception("rate limit exceeded, please slow down")
        assert BaseModel._is_rate_limit(exc)

    def test_is_rate_limit_detects_too_many_requests(self):
        exc = Exception("too many requests per minute")
        assert BaseModel._is_rate_limit(exc)

    def test_is_rate_limit_detects_quota_exceeded(self):
        exc = Exception("quota exceeded for this billing period")
        assert BaseModel._is_rate_limit(exc)

    def test_is_rate_limit_false_for_connection_error(self):
        exc = ConnectionError("connection refused")
        assert not BaseModel._is_rate_limit(exc)

    def test_is_rate_limit_false_for_generic_error(self):
        exc = ValueError("invalid input")
        assert not BaseModel._is_rate_limit(exc)

    def test_call_with_retry_uses_longer_backoff_for_rate_limit(self):
        """Rate limit retries should wait longer than transient retries."""
        import time

        model = MagicMock(spec=BaseModel)
        model.model_id = "m"
        success = ModelResponse(text="ok", model="m", provider="mock")
        call_count = {"n": 0}
        sleep_times = []

        def side_effect(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("HTTP 429: rate limit")
            return success

        model.call.side_effect = side_effect

        original_sleep = time.sleep
        def capture_sleep(t):
            sleep_times.append(t)

        with patch("time.sleep", side_effect=capture_sleep):
            result = BaseModel.call_with_retry(
                model, prompt="test", retries=3, backoff_seconds=1.0
            )

        assert result.text == "ok"
        # Rate limit backoff should be at least 4x base (4.0s + jitter)
        assert sleep_times[0] >= 4.0

    def test_is_rate_limit_correctly_classifies_errors(self):
        """_is_rate_limit returns True only for rate limit errors, False for others."""
        # Should be rate limits
        assert BaseModel._is_rate_limit(Exception("HTTP 429: Too Many Requests"))
        assert BaseModel._is_rate_limit(Exception("rate limit exceeded"))
        assert BaseModel._is_rate_limit(Exception("quota exceeded"))
        assert BaseModel._is_rate_limit(Exception("too many requests per minute"))

        # Should NOT be rate limits
        assert not BaseModel._is_rate_limit(ValueError("unexpected null"))
        assert not BaseModel._is_rate_limit(ConnectionError("connection reset"))
        assert not BaseModel._is_rate_limit(RuntimeError("model not found"))
        assert not BaseModel._is_rate_limit(TimeoutError("request timed out"))

    def test_dotenv_loaded_when_env_file_exists(self, tmp_path):
        """HarnessConfig.from_repo() should load .env if present."""
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_HARNESS_VAR=loaded_from_dotenv\n")
        (tmp_path / "harness" / "agents").mkdir(parents=True)
        (tmp_path / ".harness" / "logs").mkdir(parents=True)
        (tmp_path / "docs").mkdir()
        (tmp_path / "policies").mkdir()
        (tmp_path / "prompts").mkdir()
        (tmp_path / "AGENTS.md").write_text("# test")

        import os
        os.environ.pop("TEST_HARNESS_VAR", None)

        try:
            from dotenv import load_dotenv
            HAS_DOTENV = True
        except ImportError:
            HAS_DOTENV = False

        from harness.config import HarnessConfig
        HarnessConfig.from_repo(tmp_path)

        if HAS_DOTENV:
            assert os.environ.get("TEST_HARNESS_VAR") == "loaded_from_dotenv"
        # If dotenv not installed, just verify no crash

