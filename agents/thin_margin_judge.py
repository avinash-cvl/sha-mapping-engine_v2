"""Agent 3 -- thin-margin judge (founding_doc.md section B.6 point 3).

Called from stage_disposition.py's flow.py wiring only when
stage_disposition.needs_judge() returns True for a row. Never decides
autonomously -- it picks (or rejects) from the candidates stage_scoring.py
already ranked, and returns null rather than guessing when it can't tell.
"""
from __future__ import annotations

import json

from agents.llm_client import get_chat_model, invoke_and_audit
from common.models import MasterProduct, ScoreBreakdown, SourceProduct

_SYSTEM_PROMPT = (
    "You are checking a proposed product match. Given a source product "
    "title and up to 3 ranked Himalaya master-catalogue candidates, pick "
    "the candidate index (0-based) that is truly the same product, or "
    "null if none of them are. Consider pack size and product type "
    "carefully -- same brand and category is not enough if the pack size "
    "or product type clearly differs. Respond with strict JSON only: "
    '{"pick": int|null, "confidence": float, "reason": string}.'
)


def judge(
    source: SourceProduct,
    ranked: list[tuple[MasterProduct, ScoreBreakdown]],
) -> tuple[str | None, float, str | None, dict[str, object]]:
    """Returns (product_code, confidence, reason, audit_entry). product_code
    is "" (not None) when the LLM was asked and explicitly found no
    suitable candidate -- stage_disposition.py treats that as a distinct
    signal from "the judge wasn't called at all" (confidence stays NaN)."""
    candidates_text = "\n".join(
        f"{i}. {master.product_code} -- {master.product_name}"
        for i, (master, _) in enumerate(ranked)
    )
    prompt = f"source title: {source.title}\nbrand: {source.brand}\n\ncandidates:\n{candidates_text}"

    model = get_chat_model()
    response_text, audit_entry = invoke_and_audit(
        model,
        [("system", _SYSTEM_PROMPT), ("human", prompt)],
        call_type="thin_margin_judge",
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
