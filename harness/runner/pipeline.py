"""
pipeline.py — HarnessPipeline

Wires all six SDLC phases in order with gate checks between each.
This is the top-level orchestration layer — run the entire pipeline
with a single call, or run individual phases in sequence.

The pipeline enforces:
  - Gate checks before each phase (respects phase_gates_strict)
  - Checkpointing after each successful phase (resumes from last checkpoint)
  - Parallel execution of Layer 1 agents in the development phase
  - Decision log entries for every phase transition

Usage:
    pipeline = HarnessPipeline(config)
    pipeline.run(input_data, phases=["requirements", "design"])
    pipeline.run_all(input_data)   # runs all six phases in order
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.config import HarnessConfig


PHASE_ORDER = [
    "requirements",
    "design",
    "development",
    "testing",
    "deployment",
    "monitoring",
]


class PhaseResult:
    """Result of running one pipeline phase."""

    def __init__(
        self,
        phase: str,
        status: str,            # "pass" | "fail" | "skipped" | "gate_blocked"
        duration_seconds: float,
        agent_results: list = None,
        error: str = "",
    ):
        self.phase = phase
        self.status = status
        self.duration_seconds = duration_seconds
        self.agent_results = agent_results or []
        self.error = error
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "status": self.status,
            "duration_seconds": self.duration_seconds,
            "agent_count": len(self.agent_results),
            "error": self.error,
            "timestamp": self.timestamp,
        }


class HarnessPipeline:
    """
    Orchestrates the full SDLC harness pipeline.

    Phase runners are registered lazily — the pipeline does not import
    all agents at startup, only when a phase is actually run.
    """

    def __init__(self, config: "HarnessConfig"):
        self.config = config
        self._pipeline_log = config.logs_dir / "pipeline_log.jsonl"
        self._checkpoint = PipelineCheckpoint(config)

    def run(
        self,
        input_data: dict,
        phases: list[str] = None,
        resume: bool = True,
        stop_on_failure: bool = True,
    ) -> dict[str, PhaseResult]:
        """
        Run a subset of phases in order.

        Args:
            input_data:       Input passed to the first phase's agent
            phases:           Which phases to run (default: all)
            resume:           If True, skip phases already completed in checkpoint
            stop_on_failure:  If True, halt pipeline on first phase failure

        Returns:
            dict of phase → PhaseResult
        """
        phases = phases or PHASE_ORDER
        results: dict[str, PhaseResult] = {}

        for phase in phases:
            if phase not in PHASE_ORDER:
                print(f"  ⚠ Unknown phase '{phase}' — skipping")
                continue

            # Check checkpoint
            if resume and self._checkpoint.is_complete(phase):
                print(f"  ✓ {phase.upper()} — already complete (checkpoint), skipping")
                results[phase] = PhaseResult(phase, "skipped", 0.0)
                continue

            # Gate check
            from harness.gate import PhaseGate
            gate = PhaseGate(self.config)
            gate_result = gate.check(phase)
            if not gate_result.passed and self.config.phase_gates_strict:
                print(f"  ✗ {phase.upper()} — gate BLOCKED")
                for f in gate_result.failures:
                    print(f"      {f}")
                result = PhaseResult(phase, "gate_blocked", 0.0,
                                     error="; ".join(gate_result.failures))
                results[phase] = result
                self._log_phase(result)
                if stop_on_failure:
                    break
                continue

            # Run the phase
            print(f"\n  ▶ Running phase: {phase.upper()}")
            result = self._run_phase(phase, input_data)
            results[phase] = result
            self._log_phase(result)

            if result.status == "pass":
                self._checkpoint.mark_complete(phase)
                print(f"  ✓ {phase.upper()} complete ({result.duration_seconds:.1f}s)")
            else:
                print(f"  ✗ {phase.upper()} failed: {result.error or 'see logs'}")
                if stop_on_failure:
                    break

        return results

    def run_all(self, input_data: dict, resume: bool = True) -> dict[str, PhaseResult]:
        """Run all six phases in order."""
        return self.run(input_data, phases=PHASE_ORDER, resume=resume)

    def status(self) -> dict[str, str]:
        """Return completion status of each phase from checkpoint."""
        return {
            phase: "complete" if self._checkpoint.is_complete(phase) else "pending"
            for phase in PHASE_ORDER
        }

    def reset(self, phase: str = None) -> None:
        """Clear checkpoint for a phase (or all phases if phase=None)."""
        self._checkpoint.clear(phase)

    # ------------------------------------------------------------------
    # Phase runners
    # ------------------------------------------------------------------

    def _run_phase(self, phase: str, input_data: dict) -> PhaseResult:
        start = time.monotonic()
        try:
            if phase == "requirements":
                result = self._run_requirements(input_data)
            elif phase == "design":
                result = self._run_design(input_data)
            elif phase == "development":
                result = self._run_development(input_data)
            elif phase == "testing":
                result = self._run_testing(input_data)
            elif phase == "deployment":
                result = self._run_deployment(input_data)
            elif phase == "monitoring":
                result = self._run_monitoring(input_data)
            else:
                result = PhaseResult(phase, "fail", 0.0, error=f"No runner for '{phase}'")
        except Exception as exc:
            result = PhaseResult(phase, "fail", 0.0, error=str(exc))

        result.duration_seconds = round(time.monotonic() - start, 3)
        return result

    def _run_requirements(self, input_data: dict) -> PhaseResult:
        from harness.agents.requirements_agent import RequirementsAgent
        agent = RequirementsAgent(self.config)
        r = agent.execute(input_data)
        return PhaseResult(
            "requirements",
            "pass" if r.passed() else "fail",
            0.0,
            agent_results=[r.to_dict()],
            error="" if r.passed() else r.output.get("error", ""),
        )

    def _run_design(self, input_data: dict) -> PhaseResult:
        from harness.agents.architecture_agent import ArchitectureAgent
        agent = ArchitectureAgent(self.config)
        r = agent.execute(input_data)
        return PhaseResult(
            "design",
            "pass" if r.passed() else "fail",
            0.0,
            agent_results=[r.to_dict()],
            error="" if r.passed() else r.output.get("error", ""),
        )

    def _run_development(self, input_data: dict) -> PhaseResult:
        """Development phase: parallel specialist agents → orchestrator."""
        from harness.agents.dev_agent import OrchestratorAgent
        from harness.runner.parallel_runner import ParallelRunner

        # Discover specialist agents registered in input_data
        specialist_classes = input_data.get("specialist_agents", [])
        if not specialist_classes:
            # No specialists provided — just run orchestrator with empty results
            orch = OrchestratorAgent(self.config)
            r = orch.execute({"agent_results": [], "input_id": "pipeline"})
            return PhaseResult("development", "pass" if r.passed() else "fail",
                               0.0, agent_results=[r.to_dict()])

        specialists = [cls(self.config) for cls in specialist_classes]
        orch = OrchestratorAgent(self.config)
        runner = ParallelRunner(self.config)
        r = runner.run_orchestrated(specialists, orch, input_data)
        return PhaseResult(
            "development",
            "pass" if r.passed() else "fail",
            0.0,
            agent_results=[r.to_dict()],
        )

    def _run_testing(self, input_data: dict) -> PhaseResult:
        from harness.agents.qa_agent import QAAgent, ScenarioAgent, AdversarialAgent
        from harness.runner.parallel_runner import ParallelRunner

        agents = [
            QAAgent(self.config),
            ScenarioAgent(self.config),
            AdversarialAgent(self.config),
        ]
        runner = ParallelRunner(self.config)
        parallel_result = runner.run_parallel(agents, input_data)
        overall = "pass" if parallel_result.all_passed else "fail"
        return PhaseResult(
            "testing", overall, 0.0,
            agent_results=parallel_result.as_agent_results_list(),
            error=f"{len(parallel_result.failed)} agent(s) failed" if parallel_result.failed else "",
        )

    def _run_deployment(self, input_data: dict) -> PhaseResult:
        from harness.agents.release_agent import ReleaseAgent
        agent = ReleaseAgent(self.config)
        r = agent.execute(input_data)
        return PhaseResult(
            "deployment",
            "pass" if r.passed() else "fail",
            0.0,
            agent_results=[r.to_dict()],
        )

    def _run_monitoring(self, input_data: dict) -> PhaseResult:
        from harness.agents.gc_agent import GCAgent
        agent = GCAgent(self.config)
        r = agent.execute(input_data)
        return PhaseResult(
            "monitoring",
            "pass" if r.passed() else "fail",
            0.0,
            agent_results=[r.to_dict()],
        )

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_phase(self, result: PhaseResult) -> None:
        with self._pipeline_log.open("a") as f:
            f.write(json.dumps(result.to_dict()) + "\n")


# ---------------------------------------------------------------------------
# PipelineCheckpoint
# ---------------------------------------------------------------------------

class PipelineCheckpoint:
    """
    Tracks which phases have completed successfully.
    Stored as a simple JSON file so runs can resume after failures.
    """

    def __init__(self, config: "HarnessConfig"):
        self._path = config.logs_dir / "pipeline_checkpoint.json"
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except Exception:
                return {}
        return {}

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2))

    def is_complete(self, phase: str) -> bool:
        return self._data.get(phase, {}).get("status") == "complete"

    def mark_complete(self, phase: str) -> None:
        self._data[phase] = {
            "status": "complete",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()

    def clear(self, phase: str = None) -> None:
        if phase:
            self._data.pop(phase, None)
        else:
            self._data = {}
        self._save()

    def summary(self) -> dict[str, str]:
        return {
            phase: self._data.get(phase, {}).get("status", "pending")
            for phase in PHASE_ORDER
        }
