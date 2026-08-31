"""Row-level HGML category resolution, used to gate the candidate pool.

Returns a decision per SourceProduct. This never scores a product match --
it only narrows which master products a row is allowed to be scored against,
and short-circuits rows that are confidently not mappable at all. Every
candidate that survives the gate is scored by V1's existing code, unchanged.

Case convention: core.classify() emits HGML names in the master's own casing
("FACE WASH"), while V1's master_lookup (step_3_build_master_lookup) is keyed
on .strip().lower(). Normalise at every lookup site.
"""
from __future__ import annotations

from dataclasses import dataclass

import common.config as C
from common.models import SourceProduct
from oneds_master.category.core import TERMINALS, UNRES, CategoryPack, classify
from oneds_master.category.rules import PACKS


@dataclass(frozen=True)
class CategoryDecision:
    resolved: bool          # True -> hgml_category/hgml_subcategory are usable as a gate
    terminal: str | None    # NO HGML EQUIVALENT | UNRESOLVED | UNCLASSIFIED, else None
    hgml_category: str
    hgml_subcategory: str
    confidence: float
    evidence: str


def resolve(source: SourceProduct) -> CategoryDecision:
    """Resolve one source row to an HGML category/sub-category.

    A row with no V2 pack for its 1DS category, or whose confidence is below
    CATEGORY_GATE_MIN_CONF, comes back resolved=False and is left to V1's
    existing behaviour -- the gate only ever narrows rows it is confident
    about.
    """
    pack = PACKS.get((source.category or "").strip().lower())
    if pack is None:
        # No V2 pack for this 1DS category (the four hand-audited pilots, and
        # anything new in 1DS) -- fall through to the existing
        # config.oneds_master_category_mapping behaviour untouched.
        return CategoryDecision(False, None, "", "", 0.0, "no category pack")

    d = classify(source, pack)
    cat, sub = d["cat"], d["sub"]
    conf = float(d["sub_conf"])

    # Terminal check MUST come before the confidence comparison: classify()
    # returns 0.88 for UNCLASSIFIED, which is above the gate threshold and
    # would otherwise read as a confidently resolved category.
    if cat in TERMINALS:
        return CategoryDecision(False, cat, "", "", conf, str(d["sub_evidence"]))

    resolved = conf >= C.CATEGORY_GATE_MIN_CONF
    return CategoryDecision(
        resolved=resolved,
        terminal=None if resolved else UNRES,
        hgml_category=cat,
        hgml_subcategory=sub,
        confidence=conf,
        evidence=str(d["sub_evidence"]),
    )


def resolve_batch(
    batch,
    source_products: dict | None = None,
) -> dict:
    """Resolve a whole group's batch once, keyed by source_id.

    Mirrors how source_products/synonym_terms are computed once per group in
    main() and passed into every worker, rather than recomputed per SKU.
    Rows with no prebuilt SourceProduct are skipped -- callers fall back to
    ungated behaviour for them.
    """
    decisions: dict = {}
    for source in batch:
        source_id = getattr(source, "id", None)
        source_product = (source_products or {}).get(source_id)
        if source_product is None:
            continue
        decisions[source_id] = resolve(source_product)
    return decisions


def pack_targets(pack: CategoryPack) -> set[tuple[str, str]]:
    """Every (cat, sub) pair a pack can emit, terminals excluded.

    The tuple arities differ deliberately and must be indexed, never
    unpacked: him_lookup values are 2-tuples (cat, sub), subcat_map values
    are 3-tuples (cat, sub, conf), and default is a 4-tuple
    (cat, sub, conf, label). pack.rules holds Rule objects, which carry .cat
    and .sub attributes rather than being tuples at all.

    Used for two things: widening the eligible master pool in
    step_5_build_eligible_master_groups, and the startup assertion in main()
    that every target a pack can emit actually exists in the master.
    """
    out: set[tuple[str, str]] = set()

    for rule in pack.rules:
        out.add((rule.cat, rule.sub))

    for value in pack.him_lookup.values():
        out.add((value[0], value[1]))

    for value in pack.subcat_map.values():
        out.add((value[0], value[1]))

    out.add((pack.default[0], pack.default[1]))

    return {
        (cat, sub)
        for cat, sub in out
        if cat not in TERMINALS and sub not in TERMINALS
    }
