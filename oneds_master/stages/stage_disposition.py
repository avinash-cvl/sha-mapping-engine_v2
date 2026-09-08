"""Stage 6 -- confidence report & disposition.

Gates override averaging: a hard-incompatible type penalty already caps
the ensemble score in stage_scoring.py, so by the time a row reaches this
stage the score itself already reflects contradictions (founding_doc.md
section B.13 invariant 7). This stage's job is banded routing, plus
deciding which rows are thin-margin enough to warrant the LLM judge.
"""
from __future__ import annotations

import math
from dataclasses import replace

import common.config as C
from common.models import MasterProduct, MatchResult, ScoreBreakdown, SourceProduct


def needs_judge(ranked: list[tuple[MasterProduct, ScoreBreakdown]]) -> bool:
    """Whether to ask the LLM judge about this row.

    Now: any row that has candidates at all (see JUDGE_ALL_CANDIDATES).

    It used to be a thin-margin band -- only rows scoring in
    [TIER_REVIEW, TIER_HIGH), or with a top-1/top-2 margin under 0.05. That
    band is a small minority of rows, which meant the per-category judge
    prompts in agents/prompts/*.md were never consulted for most of the
    catalogue: a row at 0.88 was stamped Matched by arithmetic alone and a
    row at 0.48 was closed as Low Confidence, neither ever seen by the
    model. Category-specific prompting cannot improve results it is not
    invoked for.

    Product identity ("is this the SAME sellable unit?") is a judgment about
    brand, product line, formulation and pack -- exactly what the LLM is
    good at and what a weighted sum of six surface-similarity signals is
    bad at. So the judge decides, and the ensemble ranks the shortlist it
    decides from.

    Set JUDGE_ALL_CANDIDATES=false to restore the old thin-margin band (one
    LLM call per ambiguous row instead of per candidate-bearing row).
    """
    if not ranked:
        return False

    if C.JUDGE_ALL_CANDIDATES:
        return True

    top_score = ranked[0][1].ensemble
    if C.TIER_REVIEW <= top_score < C.TIER_HIGH:
        return True
    if len(ranked) > 1:
        margin = ranked[0][1].ensemble - ranked[1][1].ensemble
        if margin < 0.05 and top_score >= C.TIER_REVIEW:
            return True
    return False


def _tier_and_method(score: float, llm_confidence: float) -> tuple[str, str]:
    """Confidence tier for a row, from ONE decided score.

    `score` is the candidate's ensemble. When the judge ran, its confidence
    is what actually decides the row, so the tier must band the SAME number
    determine_mapping_status() bands -- otherwise the two labels a reviewer
    sees are computed from different inputs and disagree on the same row.
    Measured before this change, on 72 lip makeup rows: 9 rows were tiered
    "Matched" while queued as StewardReview, and 3 were tiered "Medium"
    while filed LowConfidence.

    The promotion floor is preserved: the judge's confidence may only carry
    a row the ensemble already scored respectably, but it may always demote.
    That is the same asymmetry determine_mapping_status() applies, kept in
    step so the two cannot drift apart again.
    """
    llm_ran = not math.isnan(llm_confidence)

    decided = score
    if llm_ran:
        if score >= C.LLM_PROMOTE_SCORE_FLOOR:
            decided = llm_confidence
        # Below the floor the ensemble stands on its own -- the judge's
        # confidence was never eligible to promote it.

    if decided >= C.TIER_HIGH:
        return "Matched", ("llm_confirmed" if llm_ran else "pipeline_high")
    if decided >= C.TIER_REVIEW:
        return "Medium", ("llm_confirmed" if llm_ran else "pipeline_review")
    if decided >= C.TIER_NOEQ:
        return "Low Confidence", ("llm_low" if llm_ran else "pipeline_low")
    return "No Himalaya Equivalent", ("llm_no_equivalent" if llm_ran else "pipeline_no_equivalent")


def disposition(
    source: SourceProduct,
    ranked: list[tuple[MasterProduct, ScoreBreakdown]],
    llm_pick: str | None = None,
    llm_confidence: float = float("nan"),
    llm_reason: str | None = None,
) -> MatchResult:
    """Assign the final confidence tier + resolution_method for the top
    candidate. llm_* args come from agents.thin_margin_judge, populated only
    when needs_judge() returned True for this row."""
    if not ranked:
        return MatchResult(
            source=source, candidate=None, rank=1, scores=None,
            confidence_tier="No Himalaya Equivalent", resolution_method="no_candidates",
            llm_confidence=float("nan"),
        )

    top_master, top_scores = ranked[0]

    if source.match_type == "Competitive Substitute" and top_scores.ensemble < C.TIER_NOEQ:
        return MatchResult(
            source=source, candidate=top_master, rank=1, scores=top_scores,
            confidence_tier="No Himalaya Equivalent", resolution_method="pipeline_no_equivalent",
            llm_confidence=float("nan"),
        )

    if llm_pick == "":
        # The LLM was asked and explicitly found no suitable candidate.
        #
        # llm_pick is carried through deliberately: "" is the signal that a
        # rejection HAPPENED, as distinct from the judge never running
        # (None). db._mapping_row() needs to tell those apart so it can
        # report the candidate's own similarity rather than the judge's 0.0
        # confidence -- the 0.0 is a verdict about the match, not a
        # measurement of the candidate, and a steward still needs to see how
        # close the near-miss was.
        return MatchResult(
            source=source, candidate=top_master, rank=1, scores=top_scores,
            confidence_tier="No Himalaya Equivalent", resolution_method="llm_no_equivalent",
            llm_pick=llm_pick,
            llm_confidence=llm_confidence, llm_reason=llm_reason,
        )

    # If the LLM picked a different candidate than the pre-LLM top-1, THAT
    # pick is the actual match -- both the persisted product_code and the
    # score/tier must follow it, not silently stay on the top-1 candidate
    # the LLM overrode. (This was a real bug: product_code and llm_pick
    # could disagree in the same row, with resolution_method="llm_confirmed"
    # implying agreement that hadn't actually happened.)
    winning_master, winning_scores = top_master, top_scores
    if llm_pick:
        for master, scores in ranked:
            if master.product_code == llm_pick:
                winning_master, winning_scores = master, scores
                break

    tier, method = _tier_and_method(winning_scores.ensemble, llm_confidence)
    return MatchResult(
        source=source, candidate=winning_master, rank=1, scores=winning_scores,
        confidence_tier=tier, resolution_method=method,
        llm_pick=llm_pick, llm_confidence=llm_confidence, llm_reason=llm_reason,
    )


def disposition_all(
    source: SourceProduct,
    ranked: list[tuple[MasterProduct, ScoreBreakdown]],
    llm_pick: str | None = None,
    llm_confidence: float = float("nan"),
    llm_reason: str | None = None,
) -> list[MatchResult]:
    """Same decision as disposition(), but returns every ranked candidate as
    its own row instead of only the winner -- the review UI's candidate
    grid ("Suggested matches", #2/#3 alternatives) needs these. The winner
    (whichever candidate disposition() actually decided on -- not
    necessarily ranked[0], see the LLM-override note above) is always
    persisted at rank=1; the rest keep their relative score order at
    rank=2, 3, ... with resolution_method="alternative" and no tier, since
    they were never the decision, just other options a steward can pick.
    """
    winner = disposition(source, ranked, llm_pick, llm_confidence, llm_reason)
    if not ranked:
        return [winner]

    winner_code = winner.candidate.product_code if winner.candidate else None
    alternatives = [(m, s) for m, s in ranked if m.product_code != winner_code]

    results = [replace(winner, rank=1)]
    for position, (master, scores) in enumerate(alternatives, start=2):
        results.append(MatchResult(
            source=source, candidate=master, rank=position, scores=scores,
            confidence_tier="", resolution_method="alternative",
        ))
    return results
