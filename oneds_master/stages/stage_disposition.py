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
    """Thin-margin band: top-1 score is in the ambiguous review zone, or
    top-1 vs top-2 margin is too thin to trust even a decent score."""
    if not ranked:
        return False
    top_score = ranked[0][1].ensemble
    if C.TIER_REVIEW <= top_score < C.TIER_HIGH:
        return True
    if len(ranked) > 1:
        margin = ranked[0][1].ensemble - ranked[1][1].ensemble
        if margin < 0.05 and top_score >= C.TIER_REVIEW:
            return True
    return False


def _tier_and_method(score: float, llm_confidence: float) -> tuple[str, str]:
    llm_ran = not math.isnan(llm_confidence)
    if score >= C.TIER_HIGH:
        return "Matched", ("llm_confirmed" if llm_ran else "pipeline_high")
    if score >= C.LLM_PROMOTE_SCORE_FLOOR and llm_ran and llm_confidence >= C.LLM_PROMOTE_CONFIDENCE:
        return "Matched", "llm_promoted"
    if score >= C.TIER_REVIEW:
        return "Medium", ("llm_confirmed" if llm_ran else "pipeline_review")
    if score >= C.TIER_NOEQ:
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
        return MatchResult(
            source=source, candidate=top_master, rank=1, scores=top_scores,
            confidence_tier="No Himalaya Equivalent", resolution_method="llm_no_equivalent",
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


def rerank_by_identity(
    source: SourceProduct,
    ranked: list[tuple[MasterProduct, ScoreBreakdown]],
) -> list[tuple[MasterProduct, ScoreBreakdown]]:
    """Re-order candidates by the score the portal displays.

    rank_candidates() sorts on ensemble_score. That is the right order for the
    six similarity signals, but it is not the order final_score puts them in
    once the identity layer has applied its conflicts -- and final_score is
    what the portal renders. Measured on "Litchi Shine Lip Care 4.5G (Pack Of
    5)", the best-match tile read 60 while both alternatives read 100.

    Applied before disposition() rather than after, because disposition()
    takes ranked[0] as the winner. Reordering afterwards would fix the
    displayed numbers while leaving the DECISION made on the old order --
    the tiles would look right and the mapping would still be wrong.

    The LLM's confidence is deliberately not part of the key: the judge has
    not run yet at this point, and it grades one candidate rather than
    ranking the set. Ordering on the ensemble plus the identity evidence is
    what makes a candidate whose pack facts contradict the listing fall below
    one whose facts agree.
    """
    if not C.SCORING_V2_ENABLED or len(ranked) < 2:
        return ranked

    from oneds_master.stages import stage_identity

    def _key(pair: tuple[MasterProduct, ScoreBreakdown]) -> tuple[float, float]:
        master, scores = pair
        verdict = stage_identity.evaluate(source, master)
        score = stage_identity.final_score(
            scores, verdict.attribute_score, None,
            verdict.identity_match, verdict.critical_conflict, verdict.coverage,
        )
        # ensemble breaks ties so equal scores keep a stable, reproducible
        # order rather than depending on retrieval order.
        return (score, scores.ensemble)

    return sorted(ranked, key=_key, reverse=True)


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
