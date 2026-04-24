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
    WARN:           [0, 100]  categorical — passes with noted concern, never blocks
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class Verdict(Enum):
    """Pipeline verdict with associated confidence ranges."""

    REJECTED = "REJECTED"
    NOT_SUITABLE = "NOT_SUITABLE"
    NEEDS_RESEARCH = "NEEDS_RESEARCH"
    POSSIBLE = "POSSIBLE"
    LIKELY = "LIKELY"
    SUBDIVIDE_IDEA = "SUBDIVIDE_IDEA"
    CONDITIONAL_PASS = "CONDITIONAL_PASS"
    WARN = "WARN"  # opinionated concern — passes with a note, never causes a retry
    TOO_LARGE = "TOO_LARGE"  # context window exceeded - synthesised internally, triggers subdivision

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
    Verdict.SUBDIVIDE_IDEA: (0, 100),  # categorical signal, accepts any confidence
    Verdict.CONDITIONAL_PASS: (76, 100),  # passes with noted concerns
    Verdict.WARN: (0, 100),  # categorical — opinionated concern, passes with note
    Verdict.TOO_LARGE: (100, 100),  # always 100% - synthesised on context overflow, never LLM-emitted
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

    outcome: str  # "passed" | "conditional_pass" | "rejected" | "needs_research" | "tie" | "subdivide" | "warned"
    votes: list[Vote]
    rejection_reasons: list[str] = field(default_factory=list)
    research_needed: list[str] = field(default_factory=list)
    summary: str = ""
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    has_conditional_passes: bool = False
    conditional_pass_notes: list[str] = field(default_factory=list)


def tally_votes(votes: list[Vote]) -> TallyResult:
    """Aggregate stage votes into a single pipeline outcome.

    Rules (evaluated in order):
        0. Majority of LLM stages vote SUBDIVIDE_IDEA (>=2 of 3) -> "subdivide".
        1. Any REJECTED vote -> "rejected" immediately.
        2. NOT_SUITABLE votes are abstentions — excluded from quorum entirely.
           If all votes are NOT_SUITABLE -> "passed" (no objections).
        3. Any NEEDS_RESEARCH vote (from non-abstaining votes) -> "needs_research".
        4. Equal split of pass-ish vs fail-ish (non-abstaining) -> "tie".
        5. Otherwise -> "passed" (or "conditional_pass"/"warned" if CONDITIONAL_PASS/WARN votes present).

    Categories for counting (non-abstaining only):
        fail-ish:  REJECTED
        pass-ish:  POSSIBLE, LIKELY, CONDITIONAL_PASS, WARN
        abstain:   NOT_SUITABLE (never counted toward quorum or retry thresholds)
        neutral:   NEEDS_RESEARCH (neither until resolved)

    WARN is a non-blocking opinionated concern: it is pass-ish and never causes a retry,
    but its justification is surfaced in conditional_pass_notes so the user can see it.
    """
    if not votes:
        logger.debug("Tally: 0 votes → outcome=rejected")
        return TallyResult(
            outcome="rejected",
            votes=[],
            rejection_reasons=["No votes cast"],
            summary="Pipeline rejected: no votes were cast.",
        )

    total_prompt = sum(v.prompt_tokens for v in votes)
    total_completion = sum(v.completion_tokens for v in votes)
    n = len(votes)

    # --- Rule 0: SUBDIVIDE_IDEA requires majority of LLM stages (>=2 of 3) ---
    # Static analysis never emits SUBDIVIDE_IDEA; only LLM stages can.
    subdivide_votes = [v for v in votes if v.verdict is Verdict.SUBDIVIDE_IDEA]
    llm_stage_count = sum(1 for v in votes if v.stage != "static_analysis")
    subdivide_threshold = max(2, (llm_stage_count // 2) + 1)
    if len(subdivide_votes) >= subdivide_threshold:
        logger.debug(
            "Tally: %d/%d LLM-stage subdivide_votes >= threshold %d → outcome=subdivide",
            len(subdivide_votes), llm_stage_count, subdivide_threshold,
        )
        return TallyResult(
            outcome="subdivide",
            votes=votes,
            summary=(
                f"{len(subdivide_votes)}/{llm_stage_count} LLM stages voted SUBDIVIDE_IDEA "
                f"(threshold: {subdivide_threshold})."
            ),
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
        )

    # --- Rule 1: any REJECTED -> immediate rejection ---
    rejected_votes = [v for v in votes if v.verdict is Verdict.REJECTED]
    if rejected_votes:
        logger.debug("Tally: %d votes → outcome=rejected", n)
        reasons = [f"[{v.stage}] {v.justification}" for v in rejected_votes]
        return TallyResult(
            outcome="rejected",
            votes=votes,
            rejection_reasons=reasons,
            summary=f"Pipeline rejected: {len(rejected_votes)} stage(s) voted REJECTED.",
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
        )

    # --- Rule 2: NOT_SUITABLE = abstention ---
    # Abstaining reviewers signal "this lens doesn't apply" — they are excluded from
    # quorum so an off-topic reviewer can never block or retry a design.
    voting_votes = [v for v in votes if v.verdict is not Verdict.NOT_SUITABLE]
    n_abstain = n - len(voting_votes)
    if not voting_votes:
        logger.debug("Tally: all %d votes are NOT_SUITABLE (abstain) → outcome=passed", n)
        return TallyResult(
            outcome="passed",
            votes=votes,
            summary=f"Pipeline passed: all {n} reviewer(s) abstained (NOT_SUITABLE — not applicable).",
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
        )

    # --- Rule 3: any NEEDS_RESEARCH -> needs_research ---
    # Votes tagged source="research_agent_epilogue" are excluded: they indicate
    # "investigation budget was insufficient" and must not re-spawn an agent.
    research_votes = [
        v for v in voting_votes
        if v.verdict is Verdict.NEEDS_RESEARCH
        and (v.raw_response or {}).get("source") != "research_agent_epilogue"
    ]
    if research_votes:
        logger.debug("Tally: %d votes → outcome=needs_research", n)
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

    # --- Rules 4 & 5: tie vs passed (quorum computed from voting_votes only) ---
    _FAIL_ISH = {Verdict.REJECTED}
    _PASS_ISH = {Verdict.POSSIBLE, Verdict.LIKELY, Verdict.CONDITIONAL_PASS, Verdict.WARN}

    fail_count = sum(1 for v in voting_votes if v.verdict in _FAIL_ISH)
    pass_count = sum(1 for v in voting_votes if v.verdict in _PASS_ISH)
    n_effective = len(voting_votes)

    if fail_count == pass_count and fail_count > 0:
        logger.debug("Tally: %d effective votes → outcome=tie", n_effective)
        return TallyResult(
            outcome="tie",
            votes=votes,
            summary=f"Pipeline tie: {pass_count} pass-ish vs {fail_count} fail-ish ({n_abstain} abstained).",
            total_prompt_tokens=total_prompt,
            total_completion_tokens=total_completion,
        )

    conditional_votes = [v for v in voting_votes if v.verdict is Verdict.CONDITIONAL_PASS]
    warn_votes = [v for v in voting_votes if v.verdict is Verdict.WARN]
    if conditional_votes:
        outcome = "conditional_pass"
    elif warn_votes:
        outcome = "warned"
    else:
        outcome = "passed"
    logger.debug("Tally: %d effective votes → outcome=%s", n_effective, outcome)
    cond_notes = [v.justification for v in conditional_votes] + [
        f"[WARN — {v.stage}] {v.justification}" for v in warn_votes
    ]
    return TallyResult(
        outcome=outcome,
        votes=votes,
        summary=f"Pipeline {outcome}: {pass_count}/{n_effective} voting stages passed ({n_abstain} abstained).",
        total_prompt_tokens=total_prompt,
        total_completion_tokens=total_completion,
        has_conditional_passes=bool(conditional_votes or warn_votes),
        conditional_pass_notes=cond_notes,
    )
