"""Agent -- MCDA (multi-criteria decision analysis) judge for thin-margin
matches (founding_doc.md section B.6 point 3), replacing thin_margin_judge.py
at its flow.py call site.

Called from stage_disposition.py's flow.py wiring only when
stage_disposition.needs_judge() returns True for a row -- same trigger,
same (product_code, confidence, reason, audit_entry) contract as
thin_margin_judge.judge() (kept in the repo as reference/fallback, just no
longer wired into flow.py). Where thin_margin_judge only showed the LLM
candidate names, this shows the actual per-criterion ScoreBreakdown
(semantic/lexical/category/type/pack/overlap) stage_scoring.py already
computed, so the LLM's judgment is grounded in the same criteria the
deterministic ensemble weighs, not a blind guess from titles alone.
"""
from __future__ import annotations

import json

from agents.llm_client import get_chat_model, invoke_and_audit
from common.models import MasterProduct, ScoreBreakdown, SourceProduct

    # _SYSTEM_PROMPT = (
    #     "You are checking a proposed product match using multi-criteria "
    #     "decision analysis. Given a source product title and up to 3 ranked "
    #     "Himalaya master-catalogue candidates, each with its per-criterion "
    #     "scores (semantic similarity, lexical overlap, category match, "
    #     "product-type alignment, pack-size match, token overlap -- all 0-1, "
    #     "already weighted into the ensemble score shown), weigh the criteria "
    #     "yourself and pick the candidate index (0-based) that is truly the "
    #     "same product, or null if none of them are. A high ensemble score "
    #     "with a low pack or type-alignment score is a red flag -- do not "
    #     "just pick the highest ensemble."
    #     "In the 'reason' field, always refer to products by their product_code "
    #     "(e.g. '7002677'), never by 'Candidate 0' or 'Candidate 1'. "
    #     "Respond with strict JSON only: "
    #     '{"pick": int|null, "confidence": float, "reason": string}.'
    # )
_SYSTEM_PROMPT = """You are validating whether a source product is the same product as one of up
    to 3 ranked Himalaya master-catalogue candidates.    
    Your task is PRODUCT IDENTITY MATCHING.    
    You must compare ALL candidates against the source product using the following
    criteria:    
    1. Brand alignment
    2. Product identity / product line
    3. Key active ingredient or formulation
    4. Primary benefit or use case
    5. Product type
    6. Pack size
    7. Semantic and lexical similarity    
    IMPORTANT DECISION PROCESS:    
    STEP 1 — COMPARE AND RANK    
    Evaluate every candidate against the source product across all criteria.    
    Do not reject candidates based on only one criterion such as brand mismatch.    
    Determine which candidate has the strongest overall product similarity,
    even if that candidate is ultimately not a valid exact match.    
    STEP 2 — CHECK PRODUCT IDENTITY    
    For the strongest candidate, determine whether the combined evidence is
    strong enough to conclude that it is the SAME product as the source.    
    Product identity, product line, key active ingredients/formulation, and
    primary benefit are stronger evidence than generic product type or category.    
    Generic terms such as "cleanser", "face wash", "gel", "cream", "serum",
    and "shampoo" are weak evidence and must not by themselves establish a
    product match.    
    A candidate may have strong product-type similarity but still not be the
    same product.    
    STEP 3 — SELECT OR REJECT    
    Select a candidate only when the combined evidence supports an exact or
    near-exact product identity match.    
    If the strongest candidate is only similar in category, type, or function,
    but differs materially in product identity, formulation, key ingredients,
    or primary benefit, return null.    
    CONFIDENCE DEFINITION:    
    The confidence value represents the PRODUCT MATCH STRENGTH of the best
    available candidate.    
    It is NOT confidence in the decision to reject or accept.    
    Therefore, if pick is null, confidence must represent how similar the
    strongest candidate is to the source product as a potential exact match.    
    Examples:    
    - All candidates are clearly unrelated:
    pick = null, confidence = 0.00–0.20
    
    - One candidate has similar type/category but different identity,
    ingredients, or benefit:
    pick = null, confidence = 0.20–0.49
    
    - One candidate has strong identity similarity but important ambiguity:
    pick = null, confidence = 0.50–0.74
    
    - Strong likely exact match:
    pick = candidate index, confidence = 0.75–0.89
    
    - Extremely strong exact match:
    pick = candidate index, confidence = 0.90–1.00
    
    When explaining the decision:    
    - First identify the strongest candidate by product_code.
    - Compare it directly with the source across the decisive criteria.
    - Briefly explain why it was selected or rejected.
    - Refer to candidates only by product_code.
    - Never assign a candidate product_code to the source product.
    
    Respond with strict JSON only:    
    {
    "pick": int|null,
    "confidence": float,
    "reason": string
    }
    """

def _criteria_line(scores: ScoreBreakdown) -> str:
    line = (
        f"ensemble={scores.ensemble:.2f} semantic={scores.semantic:.2f} "
        f"lexical={scores.lexical:.2f} category={scores.category:.2f} "
        f"type={scores.type_align:.2f} pack={scores.pack:.2f} "
        f"overlap={scores.overlap:.2f}"
    )
    if scores.penalty_applied:
        line += f" penalty={scores.penalty_applied}"
    return line


def judge(
    source: SourceProduct,
    ranked: list[tuple[MasterProduct, ScoreBreakdown]],
) -> tuple[str | None, float, str | None, dict[str, object]]:
    """Returns (product_code, confidence, reason, audit_entry). product_code
    is "" (not None) when the LLM was asked and explicitly found no
    suitable candidate -- stage_disposition.py treats that as a distinct
    signal from "the judge wasn't called at all" (confidence stays NaN)."""
    candidates_text = "\n".join(
        f"{i}. {master.product_code} -- {master.product_name} [{_criteria_line(scores)}]"
        for i, (master, scores) in enumerate(ranked)
    )
    prompt = f"source title: {source.clean_title}\nbrand: {source.brand}\n\ncandidates:\n{candidates_text}"

    model = get_chat_model()
    response_text, audit_entry = invoke_and_audit(
        model,
        [("system", _SYSTEM_PROMPT), ("human", prompt)],
        call_type="mcda_judge",
    )
    try:
        parsed = json.loads(response_text)
        pick_idx = parsed.get("pick")
        confidence = float(parsed.get("confidence", 0.0))
        reason = parsed.get("reason")
        if pick_idx is None:
            return "", confidence, reason, audit_entry
        product_code = ranked[pick_idx][0].product_code
        return product_code, confidence, reason, audit_entry
    except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
        audit_entry["success"] = False
        return None, float("nan"), None, audit_entry
