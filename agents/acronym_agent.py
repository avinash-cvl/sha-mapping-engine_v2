"""Agent 1 -- acronym agent (founding_doc.md section B.6 point 1, B.3).

Mining-time only, never called at runtime. Resolves the handful of
ambiguous/unknown Lexicon codes the alignment heuristic couldn't crack on
its own -- a few dozen items per mining run, with row context (division,
category, near-miss alignments) attached.

STATUS: stub. No Lexicon mining pipeline exists yet to call this from --
stage_lexicon.py is also a stub. Build that first.
"""
from __future__ import annotations

_SYSTEM_PROMPT = (
    "You resolve an ambiguous product-code acronym found in a master "
    "catalogue. Given the code and example rows it appears in, return its "
    "most likely expansion, or null if you can't tell. Respond with strict "
    'JSON only: {"expansion": string|null, "confidence": float, "evidence": string}.'
)


def resolve(code: str, example_rows: list[str]) -> dict[str, object]:
    raise NotImplementedError(
        "Acronym agent has no Lexicon mining pipeline to call it from yet -- "
        "see founding_doc.md section B.3 and stage_lexicon.py"
    )
