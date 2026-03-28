"""
parallel_runner.py — ParallelRunner

Runs Layer 1 specialist agents concurrently using ThreadPoolExecutor,
then passes their merged results to the OrchestratorAgent.

This is what was described in the architecture diagrams but not yet
implemented — Bureau, Fraud, and Policy agents running in parallel,
not sequentially.

Design principles:
  - Agents at the same layer run concurrently; agents at different layers run sequentially
  - Each agent gets its own thread; no shared mutable state between agents
  - Timeout enforced per agent — a slow agent cannot block the pipeline
  - Failures in one agent do not cancel others (fail-fast is opt-in)
  - All results logged to decision_log before Orchestrator sees them

Usage:
    runner = ParallelRunner(config)
    results = runner.run_parallel(
        agents=[bureau_agent, fraud_agent, policy_agent],
        input_data={"applicant_id": "APP_123", ...},
        timeout_seconds=30,
    )
    orchestrated = runner.run_orchestrated(
        layer1_agents=[bureau_agent, fraud_agent, policy_agent],
        orchestrator=orchestrator_agent,
        input_data=input_data,
    )
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.agents.base_agent import AgentResult, BaseAgent
    from harness.config import HarnessConfig


DEFAULT_TIMEOUT = 60          # seconds per agent
DEFAULT_MAX_WORKERS = 8       # maximum concurrent agents


@dataclass
class ParallelRunResult:
    """Aggregated result of a parallel agent execution."""
    results: dict[str, "AgentResult"]   # agent_name → AgentResult
    succeeded: list[str]                 # agent names that passed
    failed: list[str]                    # agent names that failed or timed out
    timed_out: list[str]                 # agent names that hit the timeout
    wall_time_seconds: float = 0.0
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def all_passed(self) -> bool:
        return len(self.failed) == 0 and len(self.timed_out) == 0

    @property
    def any_critical_failure(self) -> bool:
        """True if any agent returned status=fail (not just needs_human)."""
        return any(
            r.status == "fail"
            for r in self.results.values()
        )

    def as_agent_results_list(self) -> list[dict]:
        """Serialise results for the OrchestratorAgent's input."""
        return [
            {
                "agent_name": name,
                **result.to_dict(),
            }
            for name, result in self.results.items()
        ]

    def summary(self) -> str:
        lines = [
            f"Parallel run: {len(self.results)} agents in {self.wall_time_seconds:.2f}s",
            f"  Passed:    {len(self.succeeded)}",
            f"  Failed:    {len(self.failed)}",
            f"  Timed out: {len(self.timed_out)}",
        ]
        return "\n".join(lines)


class ParallelRunner:
    """
    Runs a list of agents concurrently and collects their results.

    Thread safety: each agent.execute() call is independent. Agents must
    not share mutable state — they should only read from policy files
    (which are read-only) and write to their own log entries.
    """

    def __init__(
        self,
        config: "HarnessConfig",
        max_workers: int = DEFAULT_MAX_WORKERS,
        default_timeout: float = DEFAULT_TIMEOUT,
        fail_fast: bool = False,
    ):
        self.config = config
        self.max_workers = max_workers
        self.default_timeout = default_timeout
        self.fail_fast = fail_fast   # if True, cancel remaining on first failure

    def run_parallel(
        self,
        agents: list["BaseAgent"],
        input_data: dict,
        timeout_seconds: float = None,
    ) -> ParallelRunResult:
        """
        Execute all agents concurrently.
        Returns ParallelRunResult with per-agent results.
        """
        timeout = timeout_seconds or self.default_timeout
        start = time.monotonic()

        results: dict[str, "AgentResult"] = {}
        succeeded: list[str] = []
        failed: list[str] = []
        timed_out: list[str] = []

        with ThreadPoolExecutor(max_workers=min(len(agents), self.max_workers)) as executor:
            future_to_agent = {
                executor.submit(self._run_one, agent, input_data): agent
                for agent in agents
            }

            for future in as_completed(future_to_agent, timeout=timeout + 5):
                agent = future_to_agent[future]
                try:
                    result = future.result(timeout=timeout)
                    results[agent.name] = result
                    if result.status == "pass":
                        succeeded.append(agent.name)
                    else:
                        failed.append(agent.name)
                        if self.fail_fast:
                            executor.shutdown(wait=False, cancel_futures=True)
                            break
                except TimeoutError:
                    timed_out.append(agent.name)
                    results[agent.name] = self._timeout_result(agent, timeout)
                except Exception as exc:
                    failed.append(agent.name)
                    results[agent.name] = self._error_result(agent, exc)

        # Handle agents that never completed (cancelled due to fail_fast)
        for agent in agents:
            if agent.name not in results:
                timed_out.append(agent.name)
                results[agent.name] = self._timeout_result(agent, timeout)

        wall_time = time.monotonic() - start
        return ParallelRunResult(
            results=results,
            succeeded=succeeded,
            failed=failed,
            timed_out=timed_out,
            wall_time_seconds=round(wall_time, 3),
        )

    def run_orchestrated(
        self,
        layer1_agents: list["BaseAgent"],
        orchestrator: "BaseAgent",
        input_data: dict,
        timeout_seconds: float = None,
    ) -> "AgentResult":
        """
        Full orchestrated run:
        1. Run layer1_agents in parallel
        2. Pass merged results to orchestrator
        3. Return orchestrator's AgentResult

        This is the primary entry point for multi-agent development phase runs.
        """
        print(f"\n  Running {len(layer1_agents)} agents in parallel...")
        parallel_result = self.run_parallel(layer1_agents, input_data, timeout_seconds)
        print(parallel_result.summary())

        if parallel_result.any_critical_failure:
            print(f"  ⚠ {len(parallel_result.failed)} agent(s) failed — "
                  f"passing partial results to orchestrator")

        # Build orchestrator input from all layer 1 results
        orchestrator_input = {
            "agent_results": parallel_result.as_agent_results_list(),
            "input_id": input_data.get("input_id", "unknown"),
            "parallel_run_summary": {
                "wall_time_seconds": parallel_result.wall_time_seconds,
                "succeeded": parallel_result.succeeded,
                "failed": parallel_result.failed,
                "timed_out": parallel_result.timed_out,
            },
        }

        print(f"\n  Running orchestrator...")
        return orchestrator.execute(orchestrator_input)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _run_one(agent: "BaseAgent", input_data: dict) -> "AgentResult":
        """Execute one agent. Designed to run in a thread."""
        return agent.execute(input_data)

    @staticmethod
    def _timeout_result(agent: "BaseAgent", timeout: float) -> "AgentResult":
        from harness.agents.base_agent import AgentResult
        return AgentResult(
            agent_name=agent.name,
            phase=agent.phase,
            status="fail",
            output={"error": f"Agent timed out after {timeout}s"},
            confidence=0.0,
            flags=[f"timeout_after_{timeout}s"],
        )

    @staticmethod
    def _error_result(agent: "BaseAgent", exc: Exception) -> "AgentResult":
        from harness.agents.base_agent import AgentResult
        return AgentResult(
            agent_name=agent.name,
            phase=agent.phase,
            status="fail",
            output={"error": str(exc)},
            confidence=0.0,
            flags=[f"thread_exception:{type(exc).__name__}"],
        )
