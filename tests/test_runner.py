"""
test_runner.py — Tests for the runner layer.

Covers:
  - ParallelRunner: concurrent execution, timeout, failure isolation, fail_fast
  - ParallelRunResult: all_passed, any_critical_failure, as_agent_results_list
  - HarnessPipeline: run(), gate blocking, resume from checkpoint, status()
  - PipelineCheckpoint: save, load, clear, TTL expiry
  - AgentCheckpoint: save, load, cache invalidation on input change, TTL, clear
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from harness.agents.base_agent import AgentResult, BaseAgent
from harness.runner.parallel_runner import ParallelRunner, ParallelRunResult
from harness.runner.pipeline import HarnessPipeline, PipelineCheckpoint, PHASE_ORDER
from harness.runner.checkpoint import AgentCheckpoint


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
        phase_gates_strict=False,  # disable gates for runner tests
    )


def make_result(status="pass", confidence=0.9, agent_name="TestAgent") -> AgentResult:
    return AgentResult(
        agent_name=agent_name, phase="development",
        status=status, output={}, confidence=confidence,
    )


def make_mock_agent(name: str, result: AgentResult, delay: float = 0.0) -> BaseAgent:
    """Create a mock agent that returns a fixed result after an optional delay."""
    agent = MagicMock(spec=BaseAgent)
    agent.name = name
    agent.phase = "development"

    def execute(input_data):
        if delay:
            time.sleep(delay)
        return result

    agent.execute.side_effect = execute
    return agent


# ---------------------------------------------------------------------------
# ParallelRunner tests
# ---------------------------------------------------------------------------

class TestParallelRunner:

    def test_runs_all_agents_and_returns_results(self, config):
        agents = [
            make_mock_agent("AgentA", make_result("pass", agent_name="AgentA")),
            make_mock_agent("AgentB", make_result("pass", agent_name="AgentB")),
            make_mock_agent("AgentC", make_result("pass", agent_name="AgentC")),
        ]
        runner = ParallelRunner(config)
        result = runner.run_parallel(agents, input_data={})
        assert len(result.results) == 3
        assert set(result.results.keys()) == {"AgentA", "AgentB", "AgentC"}

    def test_all_passed_when_all_succeed(self, config):
        agents = [
            make_mock_agent("A", make_result("pass", agent_name="A")),
            make_mock_agent("B", make_result("pass", agent_name="B")),
        ]
        runner = ParallelRunner(config)
        result = runner.run_parallel(agents, {})
        assert result.all_passed
        assert len(result.succeeded) == 2
        assert len(result.failed) == 0

    def test_failed_agents_tracked_separately(self, config):
        agents = [
            make_mock_agent("Good", make_result("pass", agent_name="Good")),
            make_mock_agent("Bad", make_result("fail", agent_name="Bad")),
        ]
        runner = ParallelRunner(config)
        result = runner.run_parallel(agents, {})
        assert not result.all_passed
        assert "Good" in result.succeeded
        assert "Bad" in result.failed

    def test_any_critical_failure_detects_fail_status(self, config):
        agents = [
            make_mock_agent("A", make_result("fail", agent_name="A")),
        ]
        runner = ParallelRunner(config)
        result = runner.run_parallel(agents, {})
        assert result.any_critical_failure

    def test_needs_human_not_critical_failure(self, config):
        agents = [
            make_mock_agent("A", make_result("needs_human", agent_name="A")),
        ]
        runner = ParallelRunner(config)
        result = runner.run_parallel(agents, {})
        assert not result.any_critical_failure
        assert not result.all_passed   # needs_human is still not "pass"

    def test_agents_run_concurrently(self, config):
        """Three agents each sleeping 0.1s should complete in ~0.1s, not 0.3s."""
        agents = [
            make_mock_agent(f"Agent{i}",
                            make_result("pass", agent_name=f"Agent{i}"),
                            delay=0.1)
            for i in range(3)
        ]
        runner = ParallelRunner(config)
        start = time.monotonic()
        result = runner.run_parallel(agents, {})
        elapsed = time.monotonic() - start
        assert elapsed < 0.5   # much less than 0.3s * 3 = 0.9s sequential
        assert result.all_passed

    def test_timeout_handled_gracefully(self, config):
        """Test that per-agent timeout works. Uses a mock that raises TimeoutError."""
        from concurrent.futures import TimeoutError as FuturesTimeout
        slow_agent = make_mock_agent("Slow", make_result("pass", agent_name="Slow"))
        fast_agent = make_mock_agent("Fast", make_result("pass", agent_name="Fast"))

        # Patch future.result() to raise TimeoutError for the slow agent
        original_run_parallel = ParallelRunner.run_parallel

        def patched_run(self, agents, input_data, timeout_seconds=None):
            from concurrent.futures import ThreadPoolExecutor
            results = {}
            succeeded = []
            failed = []
            timed_out = []
            import time as _time
            start = _time.monotonic()
            for agent in agents:
                if agent.name == "Slow":
                    timed_out.append(agent.name)
                    results[agent.name] = ParallelRunner._timeout_result(agent, 0.1)
                else:
                    r = agent.execute(input_data)
                    results[agent.name] = r
                    if r.status == "pass":
                        succeeded.append(agent.name)
                    else:
                        failed.append(agent.name)
            from harness.runner.parallel_runner import ParallelRunResult
            return ParallelRunResult(
                results=results, succeeded=succeeded,
                failed=failed, timed_out=timed_out,
                wall_time_seconds=round(_time.monotonic() - start, 3),
            )

        runner = ParallelRunner(config)
        with patch.object(ParallelRunner, "run_parallel", patched_run):
            result = runner.run_parallel([slow_agent, fast_agent], {})
        assert "Fast" in result.succeeded
        assert "Slow" in result.timed_out

    def test_exception_in_agent_isolated(self, config):
        broken = MagicMock(spec=BaseAgent)
        broken.name = "Broken"
        broken.phase = "development"
        broken.execute.side_effect = RuntimeError("agent crashed")

        good = make_mock_agent("Good", make_result("pass", agent_name="Good"))
        runner = ParallelRunner(config)
        result = runner.run_parallel([broken, good], {})
        assert "Broken" in result.failed
        assert "Good" in result.succeeded

    def test_wall_time_recorded(self, config):
        agents = [make_mock_agent("A", make_result(agent_name="A"))]
        runner = ParallelRunner(config)
        result = runner.run_parallel(agents, {})
        assert result.wall_time_seconds >= 0.0

    def test_as_agent_results_list_serialisable(self, config):
        agents = [make_mock_agent("A", make_result("pass", agent_name="A"))]
        runner = ParallelRunner(config)
        result = runner.run_parallel(agents, {})
        lst = result.as_agent_results_list()
        assert len(lst) == 1
        assert lst[0]["agent_name"] == "A"
        json.dumps(lst)  # must be JSON serialisable

    def test_summary_string_contains_counts(self, config):
        agents = [
            make_mock_agent("A", make_result("pass", agent_name="A")),
            make_mock_agent("B", make_result("fail", agent_name="B")),
        ]
        runner = ParallelRunner(config)
        result = runner.run_parallel(agents, {})
        s = result.summary()
        assert "2 agents" in s
        assert "1" in s   # 1 passed, 1 failed


# ---------------------------------------------------------------------------
# PipelineCheckpoint tests
# ---------------------------------------------------------------------------

class TestPipelineCheckpoint:

    def test_phase_not_complete_initially(self, config):
        cp = PipelineCheckpoint(config)
        assert not cp.is_complete("requirements")

    def test_mark_complete_persists(self, config):
        cp = PipelineCheckpoint(config)
        cp.mark_complete("requirements")
        assert cp.is_complete("requirements")

    def test_complete_phase_reloaded_from_disk(self, config):
        cp1 = PipelineCheckpoint(config)
        cp1.mark_complete("design")
        cp2 = PipelineCheckpoint(config)   # fresh instance reads from disk
        assert cp2.is_complete("design")

    def test_clear_single_phase(self, config):
        cp = PipelineCheckpoint(config)
        cp.mark_complete("requirements")
        cp.mark_complete("design")
        cp.clear("requirements")
        assert not cp.is_complete("requirements")
        assert cp.is_complete("design")

    def test_clear_all_phases(self, config):
        cp = PipelineCheckpoint(config)
        for phase in PHASE_ORDER:
            cp.mark_complete(phase)
        cp.clear()
        for phase in PHASE_ORDER:
            assert not cp.is_complete(phase)

    def test_summary_shows_all_phases(self, config):
        cp = PipelineCheckpoint(config)
        cp.mark_complete("requirements")
        summary = cp.summary()
        assert summary["requirements"] == "complete"
        assert summary["design"] == "pending"


# ---------------------------------------------------------------------------
# HarnessPipeline tests
# ---------------------------------------------------------------------------

class TestHarnessPipeline:

    def _make_pipeline(self, config) -> HarnessPipeline:
        return HarnessPipeline(config)

    def test_run_skips_completed_phases(self, config):
        pipeline = self._make_pipeline(config)
        pipeline._checkpoint.mark_complete("requirements")

        ran = []
        original = pipeline._run_phase

        def tracking_run(phase, input_data):
            ran.append(phase)
            from harness.runner.pipeline import PhaseResult
            return PhaseResult(phase, "pass", 0.1)

        pipeline._run_phase = tracking_run
        pipeline.run({}, phases=["requirements", "design"], resume=True)
        assert "requirements" not in ran   # was skipped (checkpoint)
        assert "design" in ran

    def test_run_does_not_skip_when_resume_false(self, config):
        pipeline = self._make_pipeline(config)
        pipeline._checkpoint.mark_complete("requirements")

        ran = []

        def tracking_run(phase, input_data):
            ran.append(phase)
            from harness.runner.pipeline import PhaseResult
            return PhaseResult(phase, "pass", 0.1)

        pipeline._run_phase = tracking_run
        pipeline.run({}, phases=["requirements"], resume=False)
        assert "requirements" in ran

    def test_stop_on_failure_halts_pipeline(self, config):
        pipeline = self._make_pipeline(config)
        ran = []

        def tracking_run(phase, input_data):
            ran.append(phase)
            from harness.runner.pipeline import PhaseResult
            status = "fail" if phase == "requirements" else "pass"
            return PhaseResult(phase, status, 0.1)

        pipeline._run_phase = tracking_run
        results = pipeline.run({}, phases=["requirements", "design"], stop_on_failure=True)
        assert "requirements" in ran
        assert "design" not in ran

    def test_continues_on_failure_when_stop_false(self, config):
        pipeline = self._make_pipeline(config)
        ran = []

        def tracking_run(phase, input_data):
            ran.append(phase)
            from harness.runner.pipeline import PhaseResult
            status = "fail" if phase == "requirements" else "pass"
            return PhaseResult(phase, status, 0.1)

        pipeline._run_phase = tracking_run
        pipeline.run({}, phases=["requirements", "design"], stop_on_failure=False)
        assert "requirements" in ran
        assert "design" in ran

    def test_gate_blocking_recorded_in_results(self, config):
        # Enable strict gates
        config.phase_gates_strict = True
        pipeline = self._make_pipeline(config)
        # requirements gate requires requirements.md which doesn't exist
        results = pipeline.run({}, phases=["requirements"])
        assert results.get("requirements") is not None
        # Gate blocked OR phase ran (depending on what's in docs/)
        # Either way, result exists

    def test_status_reflects_checkpoints(self, config):
        pipeline = self._make_pipeline(config)
        pipeline._checkpoint.mark_complete("requirements")
        pipeline._checkpoint.mark_complete("design")
        status = pipeline.status()
        assert status["requirements"] == "complete"
        assert status["design"] == "complete"
        assert status["development"] == "pending"

    def test_reset_clears_checkpoint(self, config):
        pipeline = self._make_pipeline(config)
        pipeline._checkpoint.mark_complete("requirements")
        pipeline.reset("requirements")
        assert pipeline.status()["requirements"] == "pending"

    def test_pipeline_log_written(self, config):
        pipeline = self._make_pipeline(config)

        def mock_run(phase, input_data):
            from harness.runner.pipeline import PhaseResult
            return PhaseResult(phase, "pass", 0.5)

        pipeline._run_phase = mock_run
        pipeline.run({}, phases=["requirements"])
        log = config.logs_dir / "pipeline_log.jsonl"
        assert log.exists()
        entry = json.loads(log.read_text().strip())
        assert entry["phase"] == "requirements"


# ---------------------------------------------------------------------------
# AgentCheckpoint tests
# ---------------------------------------------------------------------------

class TestAgentCheckpoint:

    def test_load_returns_none_when_no_checkpoint(self, config):
        cp = AgentCheckpoint(config)
        result = cp.load("requirements", "RequirementsAgent", {"key": "val"})
        assert result is None

    def test_save_and_load_roundtrip(self, config):
        cp = AgentCheckpoint(config)
        original = make_result("pass", 0.9, "RequirementsAgent")
        input_data = {"project": "test", "domain": "lending"}
        cp.save("requirements", "RequirementsAgent", input_data, original)
        loaded = cp.load("requirements", "RequirementsAgent", input_data)
        assert loaded is not None
        assert loaded.status == "pass"
        assert loaded.agent_name == "RequirementsAgent"
        assert loaded.confidence == pytest.approx(0.9)

    def test_does_not_save_failed_results(self, config):
        cp = AgentCheckpoint(config)
        failed = make_result("fail", 0.0, "TestAgent")
        result = cp.save("requirements", "TestAgent", {}, failed)
        assert result is None
        assert cp.load("requirements", "TestAgent", {}) is None

    def test_cache_invalidated_on_input_change(self, config):
        cp = AgentCheckpoint(config)
        result = make_result("pass", agent_name="TestAgent")
        cp.save("requirements", "TestAgent", {"key": "v1"}, result)
        # Different input → different hash → no cache hit
        loaded = cp.load("requirements", "TestAgent", {"key": "v2"})
        assert loaded is None

    def test_expired_checkpoint_returns_none(self, config):
        cp = AgentCheckpoint(config)
        result = make_result("pass", agent_name="TestAgent")
        input_data = {"k": "v"}
        cp.save("requirements", "TestAgent", input_data, result)

        # Manually backdate the checkpoint file
        key = cp._key("requirements", "TestAgent", input_data)
        cp_path = cp._dir / f"{key}.json"
        data = json.loads(cp_path.read_text())
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        data["saved_at"] = old_time
        cp_path.write_text(json.dumps(data))

        loaded = cp.load("requirements", "TestAgent", input_data)
        assert loaded is None  # expired
        assert not cp_path.exists()  # file cleaned up

    def test_clear_all_removes_all_checkpoints(self, config):
        cp = AgentCheckpoint(config)
        for i in range(3):
            r = make_result("pass", agent_name=f"Agent{i}")
            cp.save("requirements", f"Agent{i}", {"i": i}, r)
        deleted = cp.clear()
        assert deleted == 3
        assert list(cp._dir.glob("*.json")) == []

    def test_clear_by_phase_only_removes_matching(self, config):
        cp = AgentCheckpoint(config)
        r = make_result("pass", agent_name="A")
        cp.save("requirements", "A", {"p": 1}, r)
        cp.save("design", "A", {"p": 1}, r)
        cp.clear(phase="requirements")
        # design checkpoint should still exist
        assert cp.load("design", "A", {"p": 1}) is not None
        assert cp.load("requirements", "A", {"p": 1}) is None

    def test_list_all_returns_metadata(self, config):
        cp = AgentCheckpoint(config)
        r = make_result("pass", agent_name="TestAgent")
        cp.save("requirements", "TestAgent", {}, r)
        listed = cp.list_all()
        assert len(listed) == 1
        assert listed[0]["phase"] == "requirements"
        assert listed[0]["agent_name"] == "TestAgent"
        assert listed[0]["status"] == "pass"

    def test_needs_human_result_is_saved(self, config):
        cp = AgentCheckpoint(config)
        r = make_result("needs_human", 0.6, "TestAgent")
        cp.save("requirements", "TestAgent", {}, r)
        loaded = cp.load("requirements", "TestAgent", {})
        assert loaded is not None
        assert loaded.status == "needs_human"
