"""Stage 5 -- gated ensemble scoring.

Weighted blend of 6 signals (config.py's W_* constants -- the AHP reference
vector from founding_doc.md section B.12), then multiplicative penalties.
Gates override averaging: a hard type-incompatibility caps the score
regardless of how well everything else agrees (founding_doc.md section
B.13 invariant 7).
"""
from __future__ import annotations

import common.config as C
from oneds_competitor.stages import stage_attributes
from common.models import MasterProduct, ScoreBreakdown, SourceProduct


def _type_cluster(text: str) -> str | None:
    """Longest-match-wins vocabulary lookup, then its cluster."""
    lowered = text.lower()
    matched_term: str | None = None
    for term in C.TYPE_VOCABULARY:
        if term in lowered and (matched_term is None or len(term) > len(matched_term)):
            matched_term = term
    if matched_term is None:
        return None
    for cluster, terms in C.TYPE_CLUSTERS.items():
        if matched_term in terms:
            return cluster
    return None


def type_alignment_score(source_text: str, master_text: str) -> tuple[float, bool]:
    """Returns (score, hard_incompatible). Same cluster -> 1.0. Neither side
    classifiable -> a neutral 0.5. Different, non-conflicting clusters ->
    0.3. A hard-incompatible pair -> 0.0 with hard_incompatible=True, so the
    caller applies TYPE_HARD_INCOMPAT_PENALTY on top of the raw blend."""
    source_cluster = _type_cluster(source_text)
    master_cluster = _type_cluster(master_text)
    if source_cluster is None or master_cluster is None:
        return 0.5, False
    if source_cluster == master_cluster:
        return 1.0, False
    pair, reverse_pair = (source_cluster, master_cluster), (master_cluster, source_cluster)
    if pair in C.TYPE_HARD_INCOMPAT or reverse_pair in C.TYPE_HARD_INCOMPAT:
        return 0.0, True
    return 0.3, False


def category_score(source_subcategory: str, master_category: str) -> float:
    """Placeholder substring comparison -- TODO: replace with a cosine
    comparison of embedded subcategory/category text, matching the prior
    art's category_align signal."""
    if not source_subcategory or not master_category:
        return 0.5
    return 1.0 if source_subcategory.lower() in master_category.lower() else 0.3


def overlap_score(source: SourceProduct, master: MasterProduct) -> float:
    """Shared-keyword bonus: fraction of the source title's words that also
    appear in the master row's searchable text."""
    source_words = set(source.clean_title.split())
    master_words = set(master.text.lower().split())
    if not source_words:
        return 0.0
    return len(source_words & master_words) / len(source_words)


def _domain_mismatch(source_domain: str, master: MasterProduct) -> bool:
    """Cross-domain check (baby vs face). Division/category name is a weak
    proxy until master rows carry their own domain tag -- TODO: tag master
    rows with the same heuristic stage_ingest.py uses for source rows."""
    master_text = f"{master.division} {master.category}".lower()
    if source_domain == "baby":
        return "baby" not in master_text and "face" in master_text
    if source_domain == "face":
        return "face" not in master_text and "baby" in master_text
    return False


def score_candidate(
    source: SourceProduct,
    master: MasterProduct,
    score_semantic: float,
    score_lexical: float,
) -> ScoreBreakdown:
    type_align, hard_incompat = type_alignment_score(source.title, master.text)
    category = category_score(source.subcategory, master.category)
    pack = stage_attributes.pack_score(
        (source.pack_value, source.pack_unit) if source.pack_value is not None else None,
        (master.pack_value, master.pack_unit) if master.pack_value is not None else None,
    )
    overlap = overlap_score(source, master)

    ensemble = (
        C.W_SEMANTIC * score_semantic
        + C.W_LEXICAL * score_lexical
        + C.W_CATEGORY * category
        + C.W_TYPE * type_align
        + C.W_PACK * pack
        + C.W_OVERLAP * overlap
    )

    penalty_applied: str | None = None
    if source.domain != "other" and _domain_mismatch(source.domain, master):
        ensemble *= C.DOMAIN_MISMATCH_PENALTY
        penalty_applied = "domain_mismatch"
    if hard_incompat:
        ensemble *= C.TYPE_HARD_INCOMPAT_PENALTY
        penalty_applied = "type_hard_incompatible"

    return ScoreBreakdown(
        semantic=score_semantic, lexical=score_lexical, category=category,
        type_align=type_align, pack=pack, overlap=overlap, ensemble=ensemble,
        penalty_applied=penalty_applied,
    )


def rank_candidates(
    source: SourceProduct,
    candidates: dict[str, tuple[float, float]],
    master_by_code: dict[str, MasterProduct],
) -> list[tuple[MasterProduct, ScoreBreakdown]]:
    scored = []
    for code, (score_semantic, score_lexical) in candidates.items():
        master = master_by_code.get(code)
        if master is None:
            continue
        scored.append((master, score_candidate(source, master, score_semantic, score_lexical)))
    scored.sort(key=lambda pair: pair[1].ensemble, reverse=True)
    return scored[: C.COMPETITOR_TOP_N_OUTPUT]
