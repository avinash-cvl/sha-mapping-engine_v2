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

import functools
import json
import pathlib
import re

from agents.llm_client import get_chat_model, invoke_and_audit
from common.models import MasterProduct, ScoreBreakdown, SourceProduct

_PROMPT_DIR = pathlib.Path(__file__).parent / "prompts"

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
_GENERIC_SYSTEM_PROMPT = """You are validating whether a source product is the same product as one of up
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
    The confidence value represents how strongly the best candidate is the
    SAME SELLABLE UNIT as the source -- the same product line AND the same
    pack size/count.
    It is NOT confidence in the decision to reject or accept.
    Being the same product line in a DIFFERENT pack size or count is not a
    high-confidence match. Each candidate is given with its pack facts
    ("pack: size=... count=..."); compare them against the source pack.
    - If the pack facts disagree (e.g. source count=60 against a candidate
    count=500), cap confidence at 0.50 however well the product line matches,
    and state the disagreement in the reason.
    - If pack facts are unstated on either side, do not read that as
    agreement -- cap confidence at 0.75.
    Examples:
    - All candidates are clearly unrelated:
    pick = null, confidence = 0.00–0.20

    - One candidate has similar type/category but different identity,
    ingredients, or benefit:
    pick = null, confidence = 0.20–0.49

    - Same product line but a different pack size/count:
    pick = null, confidence = 0.30–0.50

    - One candidate has strong identity similarity but important ambiguity:
    pick = null, confidence = 0.50–0.74

    - Strong likely exact match, pack facts agree or are unstated:
    pick = candidate index, confidence = 0.75–0.89

    - Extremely strong exact match with pack size/count confirmed equal:
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


# V1's output contract, appended to every per-category prompt in place of the
# V2 "## Output" section those files ship with.
#
# This is the critical adaptation. The V2 prompts were written for a
# CLASSIFICATION task and specify `row, evidence, hgml_category,
# hgml_subcategory, confidence`. V1's judge is a CANDIDATE-SELECTION task and
# parses {"pick", "confidence", "reason"}. Letting the file's own contract
# reach the model would make every judged row fail json.loads and return NaN.
# The opening paragraph matters as much as the shape: without it the model
# still has an allowed-target list in context and tries to classify into it.
_V1_OUTPUT_CONTRACT = """## Output

You are NOT classifying into the category list above -- that list is context
for judging product identity. Your task is to decide which candidate, if any,
is the SAME product as the source.

Respond with strict JSON only:
{
"pick": int|null,
"confidence": float,
"identity_match": bool,
"brand_match": bool,
"product_family_match": bool,
"variant_match": bool,
"product_type_match": bool,
"pack_size_match": bool,
"pack_quantity_match": bool,
"critical_conflict": bool,
"reason": string
}

- `pick` is the 0-based index of the chosen candidate, or null if no candidate
  is the same product.
- `confidence` is how strongly the chosen candidate is the SAME SELLABLE UNIT
  as the source -- same product line AND same pack size/count. Being the same
  product line in a different pack size is NOT a high-confidence match. If the
  pack facts disagree (e.g. source count=60 against a candidate count=500), cap
  confidence at 0.50 no matter how well the product line matches, and say so in
  `reason`. If pack facts are unstated on either side, do not treat that as
  agreement -- cap confidence at 0.75.

Judge each attribute of your PICK separately, and answer per attribute rather
than letting one strong signal carry the rest:

- `brand_match`          same brand.
- `product_family_match` same product line -- "Tan Removal Orange" is a
                         different line from "Dark Spot Clearing Turmeric"
                         even when both are 8g sachets of the same form.
- `variant_match`        same flavour/scent/formulation variant.
- `product_type_match`   same form (cream vs wash vs tablet vs syrup).
- `pack_size_match`      same size per unit (8g vs 8g, not 100ml vs 500ml).
- `pack_quantity_match`  same number of units (12 vs 12, not 12 vs 144).
- `identity_match`       true only when this is the same sellable unit -- what
                         a shopper receives is interchangeable.
- `critical_conflict`    true when any attribute POSITIVELY disagrees, as
                         opposed to being unstated. An attribute neither side
                         mentions is unknown, not a conflict.

Use false only for a real disagreement. Where a fact is simply absent from both
texts, prefer true for that attribute and lower `confidence` instead -- absence
is uncertainty, not contradiction.

- In `reason`, refer to candidates only by product_code (e.g. '7002677'),
  never as 'Candidate 0'. Never assign a candidate's product_code to the
  source product."""


def _slug(category: str) -> str:
    """1DS category name -> prompt filename stem.

    The prompt files simply drop '&' rather than spelling it out:
    'coffee & tea' -> coffee_tea.md, 'drinks & beverages' ->
    drinks_beverages.md. Mapping ' & ' to '_and_' silently misses six of the
    25 categories, so collapse every run of non-alphanumerics instead.
    """
    return re.sub(r"[^a-z0-9]+", "_", (category or "").strip().lower()).strip("_")


@functools.lru_cache(maxsize=None)
def _system_prompt_for(category: str) -> str:
    """Per-category judge prompt, falling back to the generic one.

    Each file is a markdown document, not a raw prompt: a '# Prompt -- ...'
    heading and explanatory prose, then '---', then the system prompt itself.
    Only the four hand-audited categories carry a SECOND '---' and a trailing
    '**Note.**' paragraph -- the other 21 files have just the one fence, so
    requiring three parts would silently fall back to the generic prompt for
    21 of 25 categories. Take everything after the FIRST fence, then cut at
    the next fence if there is one.

    Finally, V2's '## Output' section is replaced with V1's contract; see
    _V1_OUTPUT_CONTRACT. Everything above it is kept -- the allowed-target
    list, decision order, hard rules and calibration table are the whole
    point of using these prompts.
    """
    path = _PROMPT_DIR / f"{_slug(category)}.md"
    if not path.exists():
        return _GENERIC_SYSTEM_PROMPT

    parts = path.read_text(encoding="utf-8").split("\n---\n")
    if len(parts) < 2:
        return _GENERIC_SYSTEM_PROMPT

    body = parts[1].split("\n---\n")[0]
    head = body.split("\n## Output")[0].strip()
    if not head:
        return _GENERIC_SYSTEM_PROMPT

    return f"{head}\n\n{_V1_OUTPUT_CONTRACT}"


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


def _pack_text(pack_value: float | None, pack_unit: str | None,
               pack_count: int | None) -> str:
    """Human-readable pack facts, or "unstated" when the row carries none.

    Sent alongside the derived pack= score because that score alone is not
    decidable evidence: the judge saw pack=0.50 for a 60-count listing against
    a 500-count master (a neutral "unknown", since the master count did not
    parse) and could not tell that from a genuine agreement. It picked the
    500's at 0.94 confidence and said so in its own reason -- "pack size
    differs only in count wording". Stating the raw numbers lets it weigh what
    it was previously being asked to take on trust.
    """
    parts: list[str] = []
    if pack_value is not None and pack_unit:
        value = f"{pack_value:g}"
        parts.append(f"size={value}{pack_unit}")
    if pack_count is not None:
        parts.append(f"count={pack_count}")
    return " ".join(parts) if parts else "unstated"


def judge(
    source: SourceProduct,
    ranked: list[tuple[MasterProduct, ScoreBreakdown]],
) -> tuple[str | None, float, str | None, dict[str, object]]:
    """Returns (product_code, confidence, reason, audit_entry). product_code
    is "" (not None) when the LLM was asked and explicitly found no
    suitable candidate -- stage_disposition.py treats that as a distinct
    signal from "the judge wasn't called at all" (confidence stays NaN)."""
    candidates_text = "\n".join(
        f"{i}. {master.product_code} -- {master.product_name} "
        f"[pack: {_pack_text(master.pack_value, master.pack_unit, master.pack_count)}] "
        f"[{_criteria_line(scores)}]"
        for i, (master, scores) in enumerate(ranked)
    )
    source_pack = _pack_text(source.pack_value, source.pack_unit, source.pack_count)
    prompt = (
        f"source title: {source.clean_title}\n"
        f"brand: {source.brand}\n"
        f"source pack: {source_pack}\n\n"
        f"candidates:\n{candidates_text}"
    )

    model = get_chat_model()
    response_text, audit_entry = invoke_and_audit(
        model,
        [("system", _system_prompt_for(source.category)), ("human", prompt)],
        call_type="mcda_judge",
    )
    try:
        parsed = json.loads(response_text)
        pick_idx = parsed.get("pick")
        confidence = float(parsed.get("confidence", 0.0))
        reason = parsed.get("reason")

        # The per-attribute verdicts ride on the audit entry rather than the
        # return tuple. judge() is called from more than one place and its
        # 4-tuple contract is relied on; widening it would touch every caller
        # to deliver something only the identity layer reads.
        #
        # .get() throughout, with no default substituted for a missing key:
        # every one of these is optional. A model that ignores the extended
        # contract, or an older prompt still in cache, yields None -- which
        # stage_identity treats as "unstated", the same as any other absent
        # fact. Nothing here is required for the judge to keep working.
        audit_entry["llm_identity"] = {
            field: parsed.get(field)
            for field in (
                "identity_match",
                "brand_match",
                "product_family_match",
                "variant_match",
                "product_type_match",
                "pack_size_match",
                "pack_quantity_match",
                "critical_conflict",
            )
        }

        if pick_idx is None:
            return "", confidence, reason, audit_entry
        product_code = ranked[pick_idx][0].product_code
        return product_code, confidence, reason, audit_entry
    except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
        audit_entry["success"] = False
        return None, float("nan"), None, audit_entry
