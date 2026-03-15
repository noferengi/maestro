"""Voting and verdict system for the Maestro intake pipeline.

Each stage of the intake pipeline (scope analysis, static analysis, feasibility,
conflict detection) casts a Vote with a Verdict and a confidence score. The
tally_votes() function aggregates all votes into a TallyResult that determines
whether a task passes intake, gets rejected, or needs further research.

Verdict thresholds (confidence -> verdict):
    REJECTED:       [0,  50]
    NOT_SUITABLE:   [51, 60]
    NEEDS_RESEARCH: [61, 75]
    POSSIBLE:       [76, 91]
    LIKELY:         [92, 100]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Verdict(Enum):
    """Pipeline verdict with associated confidence ranges."""

    REJECTED = "rejected"
    NOT_SUITABLE = "not_suitable"
    NEEDS_RESEARCH = "needs_research"
    POSSIBLE = "possible"
    LIKELY = "likely"

    @property
    def confidence_range(self) -> tuple[int, int]:
        """Return the inclusive (min, max) confidence range for this verdict."""
        return _VERDICT_RANGES[self]


_VERDICT_RANGES: dict[Verdict, tuple[int, int]] = {
    Verdict.REJECTED: (0, 50),
    Verdict.NOT_SUITABLE: (51, 60),
    Verdict.NEEDS_RESEARCH: (61, 75),
    Verdict.POSSIBLE: (76, 91),
    Verdict.LIKELY: (92, 100),
}


def classify_confidence(confidence: int) -> Verdict:
    """Map a raw confidence score (0-100) to the appropriate Verdict.

    Raises:
        ValueError: If confidence is outside the 0-100 range.
    """
    if not 0 <= confidence <= 100:
        raise ValueError(f"Confidence must be 0-100, got {confidence}")

    for verdict, (lo, hi) in _VERDICT_RANGES.items():
        if lo <= confidence <= hi:
            return verdict

    # Unreachable when ranges cover 0-100, but satisfy the type checker.
    raise ValueError(f"No verdict covers confidence {confidence}")  # pragma: no cover


def validate_vote_confidence(verdict: Verdict, confidence: int) -> None:
    """Validate that *confidence* falls within *verdict*'s allowed range.

    Raises:
        ValueError: If the confidence is out of range.
    """
    lo, hi = verdict.confidence_range
    if not lo <= confidence <= hi:
        raise ValueError(
            f"Confidence {confidence} is outside the range [{lo}, {hi}] "
            f"for verdict {verdict.value}"
        )


@dataclass(slots=True)
class Vote:
    """A single stage's verdict on an intake candidate."""

    stage: str
    verdict: Verdict
    confidence: int
    justification: str
    raw_response: dict | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""

    def __post_init__(self) -> None:
        validate_vote_confidence(self.verdict, self.confidence)


@dataclass(slots=True)
class TallyResult:
    """Aggregated result of all stage votes."""

    outcome: str  # "passed" | "rejected" | "needs_research" | "tie"
    votes: list[Vote]
    rejection_reasons: list[str] = field(default_factory=list)
    research_needed: list[str] = field(default_factory=list)
    summary: str = ""
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0


def tally_votes(votes: list[Vote]) -> TallyResult:
    """Aggregate stage votes into a single pipeline outcome.

    Rules (evaluated in order):
        1. Any REJECTED vote -> "rejected" immediately.
        2. Majority NOT_SUITABLE (>= ceil(n/2)+1 when n>=3, or >=2 when n<=3)
           -> "rejected".
        3. Any NEEDS_RESEARCH vote -> "needs_research".
        4. Equal split of pass-ish vs fail-ish -> "tie".
        5. Otherwise -> "passed".

    Categories for counting:
        fail-ish:  REJECTED, NOT_SUITABLE
        pass-ish:  POSSIBLE, LIKELY
        neutral:   NEEDS_RESEARCH (neither until resolved)
    """
    if not votes:
        return TallyResult(
            outcome="rejected",
            votes=[],
            rejection_reasons=["No votes cast"],
            summary="Pipeline rejected: no votes were cast.",
        )

    total_prompt = sum(v.prompt_tokens for v in votes)
    total_completion = sum(v.completion_tokens for v in votes)
    n = len(votes)

    # --- Rule 1: any REJECTED -> immediate rejection ---
    rejected_votes = [v for v in votes if v.verdict is Verdict.REJECTED]
    if rejected_votes:
        reasons = [f"[{v.stage}] {v.justification}" for v in rejected_votes]
        return TallyResult(
            outcome="rejected",
            votes=votes,
            rejection_reasons=reasons,
            summary=f"Pipeline rejected: {len(rejected_votes)} stage(s) voted REJECTED.",
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
        )

    # --- Rule 2: majority NOT_SUITABLE -> rejection ---
    not_suitable_votes = [v for v in votes if v.verdict is Verdict.NOT_SUITABLE]
    # Majority threshold: for 4 voters need 3+, for 3 voters need 2+, etc.
    majority_threshold = (n // 2) + 1
    if len(not_suitable_votes) >= majority_threshold:
        reasons = [f"[{v.stage}] {v.justification}" for v in not_suitable_votes]
        return TallyResult(
            outcome="rejected",
            votes=votes,
            rejection_reasons=reasons,
            summary=(
                f"Pipeline rejected: {len(not_suitable_votes)}/{n} stages "
                f"voted NOT_SUITABLE (majority)."
            ),
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
        )

    # --- Rule 3: any NEEDS_RESEARCH -> needs_research ---
    research_votes = [v for v in votes if v.verdict is Verdict.NEEDS_RESEARCH]
    if research_votes:
        stages = [v.stage for v in research_votes]
        return TallyResult(
            outcome="needs_research",
            votes=votes,
            research_needed=stages,
            summary=(
                f"Pipeline paused: {len(research_votes)} stage(s) need research "
                f"({', '.join(stages)})."
            ),
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
        )

    # --- Rules 4 & 5: tie vs passed ---
    _FAIL_ISH = {Verdict.REJECTED, Verdict.NOT_SUITABLE}
    _PASS_ISH = {Verdict.POSSIBLE, Verdict.LIKELY}

    fail_count = sum(1 for v in votes if v.verdict in _FAIL_ISH)
    pass_count = sum(1 for v in votes if v.verdict in _PASS_ISH)

    if fail_count == pass_count and fail_count > 0:
        return TallyResult(
            outcome="tie",
            votes=votes,
            summary=f"Pipeline tie: {pass_count} pass-ish vs {fail_count} fail-ish.",
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
        )

    return TallyResult(
        outcome="passed",
        votes=votes,
        summary=f"Pipeline passed: {pass_count}/{n} stages voted favourably.",
        total_prompt_tokens=total_prompt,
        total_completion_tokens=total_completion,
    )
