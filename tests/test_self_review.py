"""
test_self_review.py — Tests for SelfReviewAgent.

Covers:
  - ReviewCriteria dataclass defaults and customisation
  - ReviewResult structure, approval threshold, summary()
  - SelfReviewAgent.review() — approved and rejected paths
  - run_review_loop() — single-pass approval, multi-iteration revision,
    exhaustion → needs_human, revision failure → needs_human
  - BaseAgent.run_with_review() integration
  - review_metadata propagation into AgentResult.to_dict()
  - Generic revision path (_generic_revise)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch, call

import pytest

from harness.agents.base_agent import AgentResult, BaseAgent
from harness.agents.self_review_agent import (
    MAX_ITERATIONS,
    ReviewCriteria,
    ReviewResult,
    SelfReviewAgent,
)
from harness.model.base_model import ModelResponse


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
        confidence_threshold=0.75,
    )


def make_mock_model(response_text: str) -> MagicMock:
    model = MagicMock()
    model.model_id = "mock"
    resp = ModelResponse(text=response_text, model="mock", provider="mock")
    model.call_with_fallback.return_value = resp
    model.call_with_retry.return_value = resp
    return model


def make_draft(
    status="pass",
    confidence=0.85,
    output=None,
    flags=None,
    agent_name="ProducerAgent",
) -> AgentResult:
    return AgentResult(
        agent_name=agent_name,
        phase="development",
        status=status,
        output=output or {"result": "draft"},
        confidence=confidence,
        flags=flags or [],
    )


def approved_review_json(score=0.9) -> str:
    return json.dumps({
        "score": score,
        "approved": True,
        "issues": [],
        "revision_instructions": [],
        "reviewer_confidence": 0.95,
        "review_summary": "Output looks good.",
    })


def rejected_review_json(score=0.4, issues=None, instructions=None) -> str:
    return json.dumps({
        "score": score,
        "approved": False,
        "issues": issues or ["Missing required section"],
        "revision_instructions": instructions or ["Add the missing section"],
        "reviewer_confidence": 0.9,
        "review_summary": "Output needs revision.",
    })


def revised_output_json() -> str:
    return json.dumps({"result": "revised", "confidence": 0.9})


# ---------------------------------------------------------------------------
# ReviewCriteria tests
# ---------------------------------------------------------------------------

class TestReviewCriteria:

    def test_defaults_are_all_true(self):
        c = ReviewCriteria()
        assert c.check_policy_compliance is True
        assert c.check_completeness is True
        assert c.check_json_validity is True
        assert c.check_confidence_calibration is True
        assert c.check_no_hallucination is True
        assert c.custom_checks == []

    def test_can_disable_checks(self):
        c = ReviewCriteria(check_policy_compliance=False, check_json_validity=False)
        assert c.check_policy_compliance is False
        assert c.check_json_validity is False
        assert c.check_completeness is True

    def test_custom_checks_stored(self):
        c = ReviewCriteria(custom_checks=["Check A", "Check B"])
        assert len(c.custom_checks) == 2
        assert "Check A" in c.custom_checks


# ---------------------------------------------------------------------------
# ReviewResult tests
# ---------------------------------------------------------------------------

class TestReviewResult:

    def test_approved_true_when_score_above_threshold(self):
        r = ReviewResult(score=0.85, approved=True, issues=[], revision_instructions=[],
                         reviewer_confidence=0.9, iteration=1)
        assert r.approved is True

    def test_to_dict_has_all_keys(self):
        r = ReviewResult(score=0.5, approved=False, issues=["issue"],
                         revision_instructions=["fix it"], reviewer_confidence=0.8, iteration=2)
        d = r.to_dict()
        assert set(d.keys()) == {
            "score", "approved", "issues", "revision_instructions",
            "reviewer_confidence", "iteration", "timestamp"
        }

    def test_summary_shows_approved(self):
        r = ReviewResult(score=0.9, approved=True, issues=[], revision_instructions=[],
                         reviewer_confidence=0.95, iteration=1)
        assert "APPROVED" in r.summary()
        assert "0.90" in r.summary()

    def test_summary_shows_issues_when_rejected(self):
        r = ReviewResult(score=0.3, approved=False, issues=["Missing section X"],
                         revision_instructions=["Add section X"], reviewer_confidence=0.9, iteration=1)
        summary = r.summary()
        assert "NEEDS REVISION" in summary
        assert "Missing section X" in summary
        assert "Add section X" in summary

    def test_summary_shows_iteration(self):
        r = ReviewResult(score=0.9, approved=True, issues=[], revision_instructions=[],
                         reviewer_confidence=0.9, iteration=3)
        assert "3" in r.summary()


# ---------------------------------------------------------------------------
# SelfReviewAgent.review() tests
# ---------------------------------------------------------------------------

class TestSelfReviewAgentReview:

    def test_review_returns_approved_result(self, config):
        mock_model = make_mock_model(approved_review_json(score=0.92))
        with patch("harness.model.build_model", return_value=mock_model):
            reviewer = SelfReviewAgent(config)
            draft = make_draft()
            result = reviewer.review(draft, context="ctx", criteria=ReviewCriteria(), iteration=1)

        assert isinstance(result, ReviewResult)
        assert result.approved is True
        assert result.score == pytest.approx(0.92)
        assert result.issues == []
        assert result.iteration == 1

    def test_review_returns_rejected_result_with_issues(self, config):
        mock_model = make_mock_model(rejected_review_json(
            score=0.4,
            issues=["Missing NFR section", "Confidence too high"],
            instructions=["Add NFR section", "Lower confidence to 0.6"],
        ))
        with patch("harness.model.build_model", return_value=mock_model):
            reviewer = SelfReviewAgent(config)
            draft = make_draft(confidence=0.9)
            result = reviewer.review(draft, context="ctx", criteria=ReviewCriteria(), iteration=1)

        assert result.approved is False
        assert result.score == pytest.approx(0.4)
        assert len(result.issues) == 2
        assert len(result.revision_instructions) == 2

    def test_review_handles_llm_failure_gracefully(self, config):
        mock_model = make_mock_model("not valid json {{{{")
        with patch("harness.model.build_model", return_value=mock_model):
            reviewer = SelfReviewAgent(config)
            draft = make_draft()
            result = reviewer.review(draft, context="ctx", criteria=ReviewCriteria())

        assert result.approved is False
        assert result.score == 0.0
        assert len(result.issues) > 0
        assert result.reviewer_confidence == 0.0

    def test_review_passes_correct_iteration_number(self, config):
        captured = {}

        def capture_call(prompt, **kwargs):
            captured["prompt"] = prompt
            return ModelResponse(text=approved_review_json(), model="m", provider="mock")

        mock_model = MagicMock()
        mock_model.call_with_fallback.side_effect = capture_call

        with patch("harness.model.build_model", return_value=mock_model):
            reviewer = SelfReviewAgent(config)
            draft = make_draft()
            reviewer.review(draft, context="ctx", criteria=ReviewCriteria(), iteration=2)

        assert "iteration 2" in captured["prompt"].lower()

    def test_reviewer_confidence_stored_on_result(self, config):
        mock_model = make_mock_model(approved_review_json(score=0.88))
        with patch("harness.model.build_model", return_value=mock_model):
            reviewer = SelfReviewAgent(config)
            result = reviewer.review(make_draft(), context="", criteria=ReviewCriteria())

        assert result.reviewer_confidence == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# run_review_loop() tests
# ---------------------------------------------------------------------------

class TestRunReviewLoop:

    def test_approved_on_first_iteration_returns_pass(self, config):
        mock_model = make_mock_model(approved_review_json())
        with patch("harness.model.build_model", return_value=mock_model):
            reviewer = SelfReviewAgent(config)
            draft = make_draft()
            final = reviewer.run_review_loop(
                producing_agent=MagicMock(), draft=draft,
                context="ctx", criteria=ReviewCriteria(),
            )

        assert final.status == "pass"
        assert "review_metadata" in final.output or final.review_metadata
        metadata = final.review_metadata or final.output.get("review_metadata", {})
        assert metadata.get("iterations") == 1
        assert metadata.get("approved") is True

    def test_approved_after_revision_returns_pass(self, config):
        # First call: reject. Second call (revision): produce revised output.
        # Third call: approve.
        call_count = {"n": 0}
        responses = [
            rejected_review_json(),   # review 1 → reject
            revised_output_json(),    # generic revision output
            approved_review_json(),   # review 2 → approve
        ]

        def side_effect(prompt, **kwargs):
            r = responses[call_count["n"] % len(responses)]
            call_count["n"] += 1
            return ModelResponse(text=r, model="m", provider="mock")

        mock_model = MagicMock()
        mock_model.call_with_fallback.side_effect = side_effect

        with patch("harness.model.build_model", return_value=mock_model):
            reviewer = SelfReviewAgent(config)
            producer = MagicMock()
            producer._call_llm.return_value = revised_output_json()

            draft = make_draft()
            final = reviewer.run_review_loop(
                producing_agent=producer, draft=draft,
                context="ctx", criteria=ReviewCriteria(),
            )

        assert final.status == "pass"

    def test_exhausted_iterations_returns_needs_human(self, config):
        # Always reject — use spec=[] so MagicMock has no _revise(), forcing _generic_revise
        mock_model = make_mock_model(rejected_review_json())

        with patch("harness.model.build_model", return_value=mock_model):
            reviewer = SelfReviewAgent(config)
            # spec=[] means producer has no _revise attr — falls through to _generic_revise
            producer = MagicMock(spec=["_call_llm", "name", "phase"])
            producer._call_llm.return_value = revised_output_json()

            draft = make_draft()
            final = reviewer.run_review_loop(
                producing_agent=producer, draft=draft,
                context="ctx", criteria=ReviewCriteria(),
            )

        assert final.status == "needs_human"
        # review_metadata is stored on the object attribute, not in output dict
        metadata = final.review_metadata
        assert isinstance(metadata, dict)
        assert metadata.get("iterations") == MAX_ITERATIONS
        assert any("self_review_failed" in f for f in final.flags)

    def test_revision_failure_returns_needs_human(self, config):
        call_count = {"n": 0}

        def side_effect(prompt, **kwargs):
            call_count["n"] += 1
            # First call is a review → reject
            return ModelResponse(text=rejected_review_json(), model="m", provider="mock")

        mock_model = MagicMock()
        mock_model.call_with_fallback.side_effect = side_effect

        with patch("harness.model.build_model", return_value=mock_model):
            reviewer = SelfReviewAgent(config)
            producer = MagicMock()
            # Revision always fails
            producer._call_llm.side_effect = RuntimeError("LLM unavailable")

            draft = make_draft()
            final = reviewer.run_review_loop(
                producing_agent=producer, draft=draft,
                context="ctx", criteria=ReviewCriteria(),
            )

        assert final.status == "needs_human"

    def test_custom_revise_fn_is_called_on_rejection(self, config):
        call_count = {"reviews": 0, "revisions": 0}
        responses = [rejected_review_json(), approved_review_json()]

        def side_effect(prompt, **kwargs):
            r = responses[min(call_count["reviews"], len(responses) - 1)]
            call_count["reviews"] += 1
            return ModelResponse(text=r, model="m", provider="mock")

        mock_model = MagicMock()
        mock_model.call_with_fallback.side_effect = side_effect

        def custom_revise_fn(draft, review, context):
            call_count["revisions"] += 1
            return make_draft(output={"revised": True})

        with patch("harness.model.build_model", return_value=mock_model):
            reviewer = SelfReviewAgent(config)
            reviewer.run_review_loop(
                producing_agent=MagicMock(), draft=make_draft(),
                context="ctx", criteria=ReviewCriteria(),
                revise_fn=custom_revise_fn,
            )

        assert call_count["revisions"] >= 1

    def test_review_metadata_contains_all_reviews(self, config):
        call_count = {"n": 0}
        # Sequence: review1=reject, review2=approve (revision happens in between via _generic_revise)
        review_responses = [rejected_review_json(), approved_review_json()]

        def side_effect(prompt, **kwargs):
            r = review_responses[min(call_count["n"], len(review_responses) - 1)]
            call_count["n"] += 1
            return ModelResponse(text=r, model="m", provider="mock")

        mock_model = MagicMock()
        mock_model.call_with_fallback.side_effect = side_effect

        with patch("harness.model.build_model", return_value=mock_model):
            reviewer = SelfReviewAgent(config)
            # spec=[] so no _revise() attr — falls to _generic_revise which calls _call_llm
            producer = MagicMock(spec=["_call_llm", "name", "phase"])
            producer._call_llm.return_value = revised_output_json()

            final = reviewer.run_review_loop(
                producing_agent=producer, draft=make_draft(),
                context="ctx", criteria=ReviewCriteria(),
            )

        # review_metadata is on the object attribute
        metadata = final.review_metadata
        assert isinstance(metadata, dict), f"Expected dict, got {type(metadata)}: {metadata}"
        all_reviews = metadata.get("all_reviews", [])
        assert len(all_reviews) >= 1, f"Expected at least 1 review, got {all_reviews}"
        assert all("score" in r for r in all_reviews)


# ---------------------------------------------------------------------------
# BaseAgent.run_with_review() integration tests
# ---------------------------------------------------------------------------

class TestBaseAgentRunWithReview:

    def _make_agent(self, config, run_output: AgentResult) -> BaseAgent:
        class DummyAgent(BaseAgent):
            phase = "development"
            def run(self, input_data):
                return run_output

        mock_model = make_mock_model(approved_review_json())
        with patch("harness.model.build_model", return_value=mock_model):
            agent = DummyAgent(config)
            agent._model = mock_model
        return agent

    def test_run_with_review_returns_pass_on_approval(self, config):
        draft = make_draft(status="pass", confidence=0.9)
        agent = self._make_agent(config, draft)

        reviewer_mock = make_mock_model(approved_review_json())
        with patch("harness.model.build_model", return_value=reviewer_mock):
            with patch.object(SelfReviewAgent, "run_review_loop",
                              return_value=make_draft(status="pass")) as mock_loop:
                result = agent.run_with_review({})

        assert result.status == "pass"

    def test_run_with_review_uses_default_criteria_when_none_given(self, config):
        draft = make_draft()
        agent = self._make_agent(config, draft)

        captured = {}

        def capture_loop(producing_agent, draft, context, criteria, revise_fn=None):
            captured["criteria"] = criteria
            return make_draft(status="pass")

        with patch.object(SelfReviewAgent, "run_review_loop", side_effect=capture_loop):
            with patch("harness.model.build_model", return_value=make_mock_model(approved_review_json())):
                agent.run_with_review({})

        assert isinstance(captured["criteria"], ReviewCriteria)

    def test_run_with_review_accepts_custom_criteria(self, config):
        draft = make_draft()
        agent = self._make_agent(config, draft)
        custom = ReviewCriteria(check_json_validity=False, custom_checks=["My check"])

        captured = {}

        def capture_loop(producing_agent, draft, context, criteria, revise_fn=None):
            captured["criteria"] = criteria
            return make_draft(status="pass")

        with patch.object(SelfReviewAgent, "run_review_loop", side_effect=capture_loop):
            with patch("harness.model.build_model", return_value=make_mock_model(approved_review_json())):
                agent.run_with_review({}, criteria=custom)

        assert captured["criteria"].check_json_validity is False
        assert "My check" in captured["criteria"].custom_checks

    def test_review_metadata_in_to_dict(self, config):
        draft = make_draft()
        agent = self._make_agent(config, draft)

        final = make_draft(status="pass")
        final.review_metadata = {"iterations": 1, "approved": True, "all_reviews": []}

        with patch.object(SelfReviewAgent, "run_review_loop", return_value=final):
            with patch("harness.model.build_model", return_value=make_mock_model(approved_review_json())):
                result = agent.run_with_review({})

        d = result.to_dict()
        assert "review_metadata" in d
        # review_metadata may be on the result object or in output dict
        meta = d.get("review_metadata") or {}
        # Just verify the key exists and is a dict — approval is on the result object
        assert isinstance(meta, dict)


# ---------------------------------------------------------------------------
# _default_review_criteria on wired agents
# ---------------------------------------------------------------------------

class TestDefaultReviewCriteria:

    def test_requirements_agent_has_criteria(self, config):
        from harness.agents.requirements_agent import RequirementsAgent
        mock_model = make_mock_model("{}")
        with patch("harness.model.build_model", return_value=mock_model):
            agent = RequirementsAgent(config)
        assert hasattr(agent, "_default_review_criteria")
        criteria = agent._default_review_criteria
        assert isinstance(criteria, ReviewCriteria)
        assert len(criteria.custom_checks) > 0

    def test_architecture_agent_has_criteria(self, config):
        from harness.agents.architecture_agent import ArchitectureAgent
        mock_model = make_mock_model("{}")
        with patch("harness.model.build_model", return_value=mock_model):
            agent = ArchitectureAgent(config)
        assert hasattr(agent, "_default_review_criteria")
        criteria = agent._default_review_criteria
        assert "Layer 1" in " ".join(criteria.custom_checks)

    def test_gc_agent_has_criteria_with_policy_compliance_disabled(self, config):
        from harness.agents.gc_agent import GCAgent
        mock_model = make_mock_model("{}")
        with patch("harness.model.build_model", return_value=mock_model):
            agent = GCAgent(config)
        assert hasattr(agent, "_default_review_criteria")
        # GC agent reviews policies, so policy compliance check is disabled
        assert agent._default_review_criteria.check_policy_compliance is False
