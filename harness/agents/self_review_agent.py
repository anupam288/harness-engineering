"""
self_review_agent.py — Self-review loop for all harness agents.

The SelfReviewAgent sits between an agent's draft output and its final
AgentResult. It critiques the draft against the harness context (policy
files, architecture.md, requirements.md) and returns scored feedback with
specific revision instructions.

The producing agent uses the review to revise its output, up to MAX_ITERATIONS.
If confidence is still below threshold after all iterations, status is
set to "needs_human".

Flow:
    Agent.run() → draft AgentResult
        → SelfReviewAgent.review(draft, context, criteria)
            → ReviewResult (score, issues, revision_instructions)
        → if issues: Agent revises → new draft → review again
        → final AgentResult with review_metadata attached

Usage (in any agent's run() method):
    from harness.agents.self_review_agent import SelfReviewAgent, ReviewCriteria

    draft = self._produce_draft(input_data, context)
    reviewer = SelfReviewAgent(self.config)
    final = reviewer.run_review_loop(
        producing_agent=self,
        draft=draft,
        context=context,
        criteria=ReviewCriteria(
            check_policy_compliance=True,
            check_completeness=True,
            check_json_validity=True,
            custom_checks=["All uncertain_terms must have both 'term' and 'question' keys"],
        ),
    )
    return final
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from harness.agents.base_agent import AgentResult, BaseAgent
from harness.config import HarnessConfig

if TYPE_CHECKING:
    pass


MAX_ITERATIONS = 3   # Maximum review-revise cycles before escalating to human


# ---------------------------------------------------------------------------
# ReviewCriteria — what the reviewer checks
# ---------------------------------------------------------------------------

@dataclass
class ReviewCriteria:
    """
    Specifies what the SelfReviewAgent should check.
    Each producing agent configures its own criteria.
    """
    check_policy_compliance: bool = True      # Does output comply with policy.yaml?
    check_completeness: bool = True           # Are all required fields/sections present?
    check_json_validity: bool = True          # If output is JSON, is it well-formed?
    check_confidence_calibration: bool = True # Is stated confidence justified?
    check_no_hallucination: bool = True       # Are claims grounded in provided context?
    custom_checks: list[str] = field(default_factory=list)  # Domain-specific checks


# ---------------------------------------------------------------------------
# ReviewResult — structured critique from one review cycle
# ---------------------------------------------------------------------------

@dataclass
class ReviewResult:
    """Structured output from a single SelfReviewAgent review cycle."""
    score: float                        # 0.0 – 1.0 (1.0 = no issues)
    approved: bool                      # True if score >= approval_threshold
    issues: list[str]                   # Specific problems found
    revision_instructions: list[str]    # Actionable fixes for the producing agent
    reviewer_confidence: float          # How confident the reviewer is in this critique
    iteration: int                      # Which iteration this review covers
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "approved": self.approved,
            "issues": self.issues,
            "revision_instructions": self.revision_instructions,
            "reviewer_confidence": self.reviewer_confidence,
            "iteration": self.iteration,
            "timestamp": self.timestamp,
        }

    def summary(self) -> str:
        status = "✓ APPROVED" if self.approved else "✗ NEEDS REVISION"
        lines = [f"Review iteration {self.iteration}: {status} (score={self.score:.2f})"]
        if self.issues:
            for issue in self.issues:
                lines.append(f"  ✗ {issue}")
        if self.revision_instructions:
            lines.append("  Revision instructions:")
            for instr in self.revision_instructions:
                lines.append(f"    → {instr}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# SelfReviewAgent
# ---------------------------------------------------------------------------

class SelfReviewAgent(BaseAgent):
    """
    Reviews draft AgentResults on behalf of producing agents.

    This agent is never called directly from the CLI — it is always
    invoked by another agent via run_review_loop().

    It uses its own model entry in model_config.yaml (self_review_agent)
    which defaults to the most capable model available, since review
    quality directly determines output quality.
    """

    phase = "review"
    APPROVAL_THRESHOLD = 0.80   # Score at or above this → approved

    def run(self, input_data: dict) -> AgentResult:
        """
        Direct run() is not the normal path — use run_review_loop() instead.
        Kept for interface compliance and testing.
        """
        return self._do_review(
            draft_output=input_data.get("draft_output", {}),
            context=input_data.get("context", ""),
            criteria_dict=input_data.get("criteria", {}),
            iteration=input_data.get("iteration", 1),
        )

    def review(
        self,
        draft: AgentResult,
        context: str,
        criteria: ReviewCriteria,
        iteration: int = 1,
    ) -> ReviewResult:
        """
        Review a single draft AgentResult.
        Returns a ReviewResult with score, issues, and revision instructions.
        """
        result = self._do_review(
            draft_output=draft.output,
            context=context,
            criteria_dict=self._criteria_to_dict(criteria),
            iteration=iteration,
            producing_agent=draft.agent_name,
            draft_confidence=draft.confidence,
            draft_flags=draft.flags,
        )

        # Parse the LLM review from result.output
        return ReviewResult(
            score=float(result.output.get("score", 0.5)),
            approved=result.output.get("approved", False),
            issues=result.output.get("issues", []),
            revision_instructions=result.output.get("revision_instructions", []),
            reviewer_confidence=result.output.get("reviewer_confidence", 0.7),
            iteration=iteration,
        )

    def run_review_loop(
        self,
        producing_agent: BaseAgent,
        draft: AgentResult,
        context: str,
        criteria: ReviewCriteria,
        revise_fn: callable = None,
    ) -> AgentResult:
        """
        Run the full review-revise loop for a producing agent.

        Args:
            producing_agent: The agent whose output is being reviewed
            draft:           Initial draft AgentResult
            context:         The context string the producing agent used
            criteria:        What to check in the review
            revise_fn:       Optional callable(draft, review, context) → AgentResult
                             If None, the producing agent's _revise() method is called

        Returns:
            Final AgentResult with review_metadata attached.
            If all iterations fail: status="needs_human".
        """
        reviews: list[ReviewResult] = []
        current_draft = draft

        for iteration in range(1, MAX_ITERATIONS + 1):
            review = self.review(
                draft=current_draft,
                context=context,
                criteria=criteria,
                iteration=iteration,
            )
            reviews.append(review)

            print(review.summary())

            if review.approved:
                # Attach review metadata and return
                return self._attach_review_metadata(current_draft, reviews, "pass")

            if iteration == MAX_ITERATIONS:
                # Exhausted iterations — escalate
                break

            # Revise and try again
            try:
                if revise_fn:
                    current_draft = revise_fn(current_draft, review, context)
                elif hasattr(producing_agent, "_revise"):
                    current_draft = producing_agent._revise(current_draft, review, context)
                else:
                    # Generic revision: re-call the agent with review instructions appended
                    current_draft = self._generic_revise(
                        producing_agent, current_draft, review, context
                    )
            except Exception as exc:
                # Revision failed — attach what we have and escalate
                current_draft.flags.append(f"revision_failed: {exc}")
                break

        # All iterations exhausted without approval → needs_human
        return self._attach_review_metadata(current_draft, reviews, "needs_human")

    # ------------------------------------------------------------------
    # Internal review execution
    # ------------------------------------------------------------------

    def _do_review(
        self,
        draft_output: dict,
        context: str,
        criteria_dict: dict,
        iteration: int,
        producing_agent: str = "unknown",
        draft_confidence: float = 0.0,
        draft_flags: list = None,
    ) -> AgentResult:
        """Execute one LLM review call."""
        # Defensive coercion — inputs may come from tests with MagicMock values
        try:
            draft_confidence = float(draft_confidence)
        except (TypeError, ValueError):
            draft_confidence = 0.0
        if not isinstance(draft_flags, list):
            draft_flags = []
        try:
            draft_output = dict(draft_output)
        except (TypeError, ValueError):
            draft_output = {"raw": str(draft_output)}

        # Safe serialization — draft_output may contain non-JSON-serialisable objects
        try:
            draft_output_str = json.dumps(draft_output, indent=2, default=str)
        except Exception:
            draft_output_str = str(draft_output)

        prompt = f"""
{context}

=== SELF-REVIEW TASK ===
You are the SelfReviewAgent. Review the following draft output from {producing_agent}.
This is review iteration {iteration} of {MAX_ITERATIONS}.

=== DRAFT OUTPUT ===
{draft_output_str}

Producing agent confidence: {float(draft_confidence):.2f}
Producing agent flags: {json.dumps(draft_flags or [])}

=== REVIEW CRITERIA ===
{json.dumps(criteria_dict, indent=2)}

=== INSTRUCTIONS ===
Evaluate the draft output against:
1. The policy files and architecture rules in context above
2. The review criteria specified
3. Internal consistency (does the output contradict itself?)
4. Completeness (are all required sections/fields present?)
5. Grounding (are all claims supported by the context provided?)

Return ONLY valid JSON with these keys:
{{
  "score": float,                      // 0.0-1.0 (1.0 = no issues at all)
  "approved": bool,                    // true if score >= {self.APPROVAL_THRESHOLD}
  "issues": [str],                     // specific problems found, empty if none
  "revision_instructions": [str],      // actionable fixes, one per issue
  "reviewer_confidence": float,        // 0.0-1.0: how thorough was this review?
  "review_summary": str                // 1-2 sentence plain-english summary
}}

Be specific. "Output is incomplete" is not useful.
"requirements_md is missing the Non-Functional Requirements section" is useful.
"""

        try:
            response_text = self._call_llm(prompt)
            parsed = json.loads(response_text)
            return AgentResult(
                agent_name=self.name,
                phase=self.phase,
                status="pass",
                output=parsed,
                confidence=float(parsed.get("reviewer_confidence", 0.7)),
            )
        except Exception as exc:
            return AgentResult(
                agent_name=self.name,
                phase=self.phase,
                status="fail",
                output={
                    "score": 0.0,
                    "approved": False,
                    "issues": [f"Review LLM call failed: {exc}"],
                    "revision_instructions": ["Retry the review"],
                    "reviewer_confidence": 0.0,
                    "review_summary": f"Review failed: {exc}",
                },
                confidence=0.0,
                flags=["review_llm_failed"],
            )

    def _generic_revise(
        self,
        producing_agent: BaseAgent,
        draft: AgentResult,
        review: ReviewResult,
        context: str,
    ) -> AgentResult:
        """
        Generic revision: re-call the producing agent's LLM with
        the original context + review instructions appended.
        """
        revision_prompt = f"""
{context}

=== REVISION INSTRUCTIONS ===
Your previous draft was reviewed and found to have the following issues.
Fix ALL of them in your revised output.

Issues found:
{json.dumps(review.issues, indent=2)}

Specific revision instructions:
{json.dumps(review.revision_instructions, indent=2)}

Previous draft output (for reference):
{json.dumps(draft.output, indent=2)}

Produce a corrected version. Return ONLY valid JSON in the same format as before.
"""
        try:
            revised_text = producing_agent._call_llm(revision_prompt)
            revised_output = json.loads(revised_text)
            # Return a new AgentResult with the revised output
            return AgentResult(
                agent_name=draft.agent_name,
                phase=draft.phase,
                status="pass",
                output=revised_output,
                confidence=min(draft.confidence + 0.05, 1.0),  # slight bump on revision
                artifacts_produced=draft.artifacts_produced,
                flags=draft.flags + [f"revised_after_iteration_{review.iteration}"],
            )
        except Exception as exc:
            raise RuntimeError(f"Generic revision failed: {exc}") from exc

    def _attach_review_metadata(
        self,
        result: AgentResult,
        reviews: list[ReviewResult],
        final_status: str,
    ) -> AgentResult:
        """Attach full review history to the AgentResult — both attribute and output dict."""
        result.status = final_status
        metadata = {
            "iterations": len(reviews),
            "final_score": reviews[-1].score if reviews else 0.0,
            "approved": reviews[-1].approved if reviews else False,
            "all_reviews": [r.to_dict() for r in reviews],
        }
        # Store on both the object attribute AND the output dict so callers
        # can access it whether they call run_review_loop() directly or via
        # run_with_review() (which pops it from output onto the attribute).
        result.review_metadata = metadata
        result.output["review_metadata"] = metadata
        if final_status == "needs_human":
            result.flags.append(
                f"self_review_failed_after_{len(reviews)}_iterations"
            )
        return result

    @staticmethod
    def _criteria_to_dict(criteria: ReviewCriteria) -> dict:
        return {
            "check_policy_compliance": criteria.check_policy_compliance,
            "check_completeness": criteria.check_completeness,
            "check_json_validity": criteria.check_json_validity,
            "check_confidence_calibration": criteria.check_confidence_calibration,
            "check_no_hallucination": criteria.check_no_hallucination,
            "custom_checks": criteria.custom_checks,
        }
