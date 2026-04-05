"""
Tests for app/agent/verdicts.py - tally_votes() rules and Vote validation.
Pure unit tests: no DB, no LLM, no mocking.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.agent.verdicts import Vote, Verdict, TallyResult, tally_votes, validate_vote_confidence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vote(verdict: Verdict, stage: str = "test", justification: str = "ok") -> Vote:
    """Create a Vote with a valid confidence for the given verdict."""
    lo, hi = verdict.confidence_range
    confidence = (lo + hi) // 2  # mid-point is always in range
    return Vote(stage=stage, verdict=verdict, confidence=confidence, justification=justification)


# ---------------------------------------------------------------------------
# Rule 0 - SUBDIVIDE_IDEA
# ---------------------------------------------------------------------------

class TestRule0:
    def test_single_subdivide_vote(self):
        votes = [_vote(Verdict.SUBDIVIDE_IDEA)]
        result = tally_votes(votes)
        assert result.outcome == "subdivide"

    def test_subdivide_beats_rejected(self):
        """Rule 0 overrides Rule 1: SUBDIVIDE_IDEA takes priority over REJECTED."""
        votes = [
            _vote(Verdict.SUBDIVIDE_IDEA, stage="scope"),
            _vote(Verdict.REJECTED, stage="conflict"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "subdivide"

    def test_subdivide_beats_needs_research(self):
        votes = [
            _vote(Verdict.SUBDIVIDE_IDEA, stage="scope"),
            _vote(Verdict.NEEDS_RESEARCH, stage="feasibility"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "subdivide"


# ---------------------------------------------------------------------------
# Rule 1 - REJECTED
# ---------------------------------------------------------------------------

class TestRule1:
    def test_single_rejected(self):
        votes = [_vote(Verdict.REJECTED)]
        result = tally_votes(votes)
        assert result.outcome == "rejected"
        assert len(result.rejection_reasons) == 1

    def test_rejected_with_likely(self):
        """One REJECTED among passing votes still triggers Rule 1."""
        votes = [
            _vote(Verdict.LIKELY, stage="scope"),
            _vote(Verdict.REJECTED, stage="feasibility"),
            _vote(Verdict.LIKELY, stage="conflict"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "rejected"


# ---------------------------------------------------------------------------
# Rule 2 - Majority NOT_SUITABLE
# ---------------------------------------------------------------------------

class TestRule2:
    def test_majority_not_suitable(self):
        """3 of 4 votes NOT_SUITABLE triggers Rule 2 (majority_threshold = 3)."""
        votes = [
            _vote(Verdict.NOT_SUITABLE, stage="s1"),
            _vote(Verdict.NOT_SUITABLE, stage="s2"),
            _vote(Verdict.NOT_SUITABLE, stage="s3"),
            _vote(Verdict.LIKELY, stage="s4"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "rejected"

    def test_not_majority_not_suitable(self):
        """2 of 5 NOT_SUITABLE does NOT trigger Rule 2 (majority_threshold = 3)."""
        votes = [
            _vote(Verdict.NOT_SUITABLE, stage="s1"),
            _vote(Verdict.NOT_SUITABLE, stage="s2"),
            _vote(Verdict.LIKELY, stage="s3"),
            _vote(Verdict.LIKELY, stage="s4"),
            _vote(Verdict.POSSIBLE, stage="s5"),
        ]
        result = tally_votes(votes)
        assert result.outcome != "rejected"

    def test_two_of_two_not_suitable(self):
        """2 of 2 NOT_SUITABLE: majority_threshold = 2, so Rule 2 fires."""
        votes = [
            _vote(Verdict.NOT_SUITABLE, stage="s1"),
            _vote(Verdict.NOT_SUITABLE, stage="s2"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "rejected"


# ---------------------------------------------------------------------------
# Rule 3 - NEEDS_RESEARCH
# ---------------------------------------------------------------------------

class TestRule3:
    def test_needs_research_outcome(self):
        votes = [
            _vote(Verdict.LIKELY, stage="scope"),
            _vote(Verdict.NEEDS_RESEARCH, stage="feasibility"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "needs_research"
        assert "feasibility" in result.research_needed

    def test_multiple_needs_research_stages(self):
        votes = [
            _vote(Verdict.NEEDS_RESEARCH, stage="scope"),
            _vote(Verdict.NEEDS_RESEARCH, stage="feasibility"),
            _vote(Verdict.LIKELY, stage="conflict"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "needs_research"
        assert "scope" in result.research_needed
        assert "feasibility" in result.research_needed


# ---------------------------------------------------------------------------
# Rule 4 - Tie
# ---------------------------------------------------------------------------

class TestRule4:
    def test_equal_pass_fail_split(self):
        """Equal pass-ish vs fail-ish counts with no NEEDS_RESEARCH triggers tie."""
        votes = [
            _vote(Verdict.LIKELY, stage="scope"),
            _vote(Verdict.NOT_SUITABLE, stage="conflict"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "tie"

    def test_two_two_split(self):
        votes = [
            _vote(Verdict.LIKELY, stage="s1"),
            _vote(Verdict.POSSIBLE, stage="s2"),
            _vote(Verdict.NOT_SUITABLE, stage="s3"),
            _vote(Verdict.NOT_SUITABLE, stage="s4"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "tie"


# ---------------------------------------------------------------------------
# Rule 5 - Passed / Conditional pass
# ---------------------------------------------------------------------------

class TestRule5:
    def test_majority_likely_passes(self):
        votes = [
            _vote(Verdict.LIKELY, stage="scope"),
            _vote(Verdict.LIKELY, stage="feasibility"),
            _vote(Verdict.POSSIBLE, stage="conflict"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "passed"

    def test_conditional_pass(self):
        votes = [
            _vote(Verdict.LIKELY, stage="scope"),
            _vote(Verdict.CONDITIONAL_PASS, stage="feasibility"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "conditional_pass"
        assert result.has_conditional_passes is True


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_votes(self):
        """No votes -> rejected (degenerate base case)."""
        result = tally_votes([])
        assert result.outcome == "rejected"
        assert "No votes cast" in result.rejection_reasons

    def test_single_likely(self):
        votes = [_vote(Verdict.LIKELY)]
        result = tally_votes(votes)
        assert result.outcome == "passed"

    def test_token_totals_summed(self):
        v1 = Vote(stage="s1", verdict=Verdict.LIKELY, confidence=95,
                  justification="ok", prompt_tokens=10, completion_tokens=20)
        v2 = Vote(stage="s2", verdict=Verdict.LIKELY, confidence=92,
                  justification="ok", prompt_tokens=5, completion_tokens=15)
        result = tally_votes([v1, v2])
        assert result.total_prompt_tokens == 15
        assert result.total_completion_tokens == 35


# ---------------------------------------------------------------------------
# Vote confidence validation
# ---------------------------------------------------------------------------

class TestVoteConfidenceValidation:
    def test_valid_confidence_in_range(self):
        # Should not raise
        v = Vote(stage="s", verdict=Verdict.LIKELY, confidence=95, justification="ok")
        assert v.confidence == 95

    def test_confidence_below_range_raises(self):
        with pytest.raises(ValueError):
            Vote(stage="s", verdict=Verdict.LIKELY, confidence=50, justification="bad")

    def test_confidence_above_range_raises(self):
        # REJECTED range is [0, 50], so 51 is invalid
        with pytest.raises(ValueError):
            Vote(stage="s", verdict=Verdict.REJECTED, confidence=51, justification="bad")

    def test_validate_vote_confidence_helper(self):
        with pytest.raises(ValueError):
            validate_vote_confidence(Verdict.LIKELY, 50)  # LIKELY range is [92, 100]


# ---------------------------------------------------------------------------
# Rule 3 - source guard: research_agent_epilogue votes skip research spawn
# ---------------------------------------------------------------------------

class TestRule3EpilogueSourceGuard:
    def test_needs_research_from_epilogue_does_not_trigger_research_spawn(self):
        """A NEEDS_RESEARCH vote tagged source=research_agent_epilogue must not produce needs_research outcome."""
        epilogue_vote = Vote(
            stage="research_epilogue",
            verdict=Verdict.NEEDS_RESEARCH,
            confidence=70,
            justification="budget exhausted",
            raw_response={"source": "research_agent_epilogue"},
        )
        likely_vote = Vote(
            stage="scope",
            verdict=Verdict.LIKELY,
            confidence=95,
            justification="looks fine",
        )
        result = tally_votes([epilogue_vote, likely_vote])
        # The epilogue NEEDS_RESEARCH is filtered; only the LIKELY vote counts -> passed
        assert result.outcome != "needs_research"

    def test_untagged_needs_research_still_triggers_research_spawn(self):
        """A NEEDS_RESEARCH vote without source tag must still produce needs_research outcome."""
        normal_vote = Vote(
            stage="feasibility",
            verdict=Verdict.NEEDS_RESEARCH,
            confidence=70,
            justification="need more info",
            raw_response={"source": "some_other_source"},
        )
        result = tally_votes([normal_vote])
        assert result.outcome == "needs_research"

    def test_needs_research_with_no_raw_response_still_triggers(self):
        """A NEEDS_RESEARCH vote with raw_response=None must still produce needs_research outcome."""
        vote = Vote(
            stage="feasibility",
            verdict=Verdict.NEEDS_RESEARCH,
            confidence=70,
            justification="need more info",
            raw_response=None,
        )
        result = tally_votes([vote])
        assert result.outcome == "needs_research"
