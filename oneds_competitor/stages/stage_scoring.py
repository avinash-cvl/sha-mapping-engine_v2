"""Stage 5 -- gated ensemble scoring.

Weighted blend of 6 signals (config.py's W_* constants -- the AHP reference
vector from founding_doc.md section B.12), then multiplicative penalties.
Gates override averaging: a hard type-incompatibility caps the score
regardless of how well everything else agrees (founding_doc.md section
B.13 invariant 7).
"""
from __future__ import annotations

import re

import common.config as C
from oneds_competitor.stages import stage_attributes
from common.models import MasterProduct, ScoreBreakdown, SourceProduct

# Form/measure words carry no identifying power inside a product group, so
# they are dropped before the token-subset test in product_group_match().
# Without this a group of "LIP BALM" would match every lip balm listing and
# hand out the bonus indiscriminately -- the bonus is only meaningful for the
# distinctive part of the name ("strawberry shine").
# Kept deliberately short. A word only belongs here if it names a FORM that
# many different products share -- "butter" looks like one but is product
# identity ("COCOA BUTTER LIP BALM"), and dropping it made a Rich Cocoa
# Butter listing fail to match its own group.
_GROUP_STOPWORDS = frozenset({
    "lip", "balm", "care", "cream", "creams", "wash", "face", "body", "hair",
    "oil", "gel", "soap", "powder", "lotion", "shampoo", "serum", "mask",
    "scrub", "toner", "kit", "pack", "wipes", "baby", "men", "mens",
    "the", "and", "&", "with", "of", "for", "-",
})

# Words a listing may drop or add without changing which product line it is.
# "Rich Cocoa Butter" and "Cocoa Butter" are the same line; so are
# "Himalaya Purifying Neem" and "Purifying Neem". Stripped from BOTH sides
# before the subset test so the comparison is symmetric.
_GROUP_FILLER = frozenset({"rich", "natural", "herbals", "herbal", "himalaya", "new"})

# Generic marketing/quality adjectives -- a BENEFIT many unrelated product
# lines share, not an identity any one of them owns. Unlike _GROUP_STOPWORDS
# (form nouns: "cream", "wash") these still carry real distinguishing power
# within their OWN group's full name ("GENTLE BABY WIPES" is a specific,
# correctly-matchable product), so they cannot simply join that list --
# doing that once regressed 236 currently-correct matches whose product_group
# has no identity beyond one of these words plus a form noun. The failure
# mode this list exists for is narrower: a group that reduces to ONLY one of
# these words, with nothing else surviving _GROUP_STOPWORDS, is too generic
# to identify a product on its own -- "REFRESHING TONER" reduces to
# {"refreshing"}, which is also true of "Refreshing Cleansing Milk", a
# completely different form of product that happens to share the adjective.
# See product_group_match() for where this list is actually consulted.
_GROUP_GENERIC_ADJ = frozenset({
    "refreshing", "gentle", "nourishing", "hydrating", "moisturizing",
    "moisturising", "soothing", "revitalizing", "rejuvinating", "toning",
    "regular", "na", "null",
})

# Explicit multipack cues in a listing title. Mirrors the notation
# category/core.py's pack_count() reads, kept local so stage_scoring does not
# depend on the vendored category package.
_MULTIPACK_CUE = re.compile(
    r"\bpack of \d|\bset of \d|\bcombo\b|\d\s*[x*]\s*\d|\b\d+\s*n\b|"
    r"\b\d+\s*pcs?\b|\btwin pack\b|\bmulti ?pack\b"
)


def _type_clusters(text: str) -> frozenset[str]:
    """Every cluster whose vocabulary a term in `text` matches -- not just the
    longest one.

    Was longest-match-wins, one cluster only: "Diaper Rash Cream" matches
    both "diaper rash" (11 chars, cluster diaper) and "cream" (5 chars,
    cluster cream), and length picked "diaper" alone. A title that
    legitimately names two clusters at once (a diaper-rash product IS a
    cream; a hair cream IS both "hair" and "cream") was then read as though
    it were only ever one -- so it either missed the 1.0 same-cluster score
    against a same-type candidate that happened to phrase itself the other
    way, or worse, took a hard-incompatible 0.0 against it. Measured on
    blinkit 559220: "DIAPER RASH CREAM" (which ALSO reads as pure "diaper")
    outscored "BABY RASH RELIEF CREAM WITH PURE COW GHEE" (reads as "cream"
    only, no "diaper" in ITS title) by a full type_align gap, 1.0 to 0.3,
    despite the source genuinely being both.

    Returns every matched cluster, not just one -- type_alignment_score
    below then asks "do these two sets share anything" instead of "are
    these two single labels equal", which is the actual question a product
    that spans two vocabulary terms needs asked.
    """
    lowered = text.lower()
    clusters: set[str] = set()
    for cluster, terms in C.TYPE_CLUSTERS.items():
        for term in terms:
            if term in lowered:
                clusters.add(cluster)
                break
    return frozenset(clusters)


def _master_type_clusters(master_text: str, master_title: str | None = None) -> frozenset[str]:
    """The master's product type(s), read from its TITLE in preference to its
    search_text.

    search_text has the row's own category and subcategory appended to it by
    the ingest pipeline, and a category tail can name a cluster the product
    itself is not. Measured: "FACE CLEANSERS EXCL. FACE WASH" contains
    "face cleanser", which used to beat "face mask" on longest-match and
    classified every sheet mask, scrub and face pack in that category as
    "wash" outright. Now that a text can carry more than one cluster, the
    title's own clusters still take priority over search_text's when the
    title yields any at all -- the category tail is additive noise, not a
    correction, and letting it inject an extra cluster the product doesn't
    have would just trade one kind of wrong overlap for another.

    Falls back to search_text only when the title yields no cluster at all:
    269 master rows carry abbreviated titles ("CCTP 175G + GTB", "PSSL 100ml+
    PNFW") whose type is only recoverable from the expanded search_text.
    """
    from_title = _type_clusters(master_title) if master_title else frozenset()
    if from_title:
        return from_title
    return _type_clusters(master_text)


def type_alignment_score(
    source_text: str,
    master_text: str,
    master_title: str | None = None,
) -> tuple[float, bool]:
    """Returns (score, hard_incompatible). Any shared cluster -> 1.0. Neither
    side classifiable -> a neutral 0.5. Disjoint, non-conflicting cluster
    sets -> 0.3. Every pairing across two hard-incompatible cluster sets ->
    0.0 with hard_incompatible=True, so the caller applies
    TYPE_HARD_INCOMPAT_PENALTY on top of the raw blend.

    "Shared cluster" rather than "equal cluster": a text can belong to more
    than one cluster at once (a diaper rash cream is both "diaper" and
    "cream"), so the comparison is a set intersection, not an equality test.
    A hard-incompat pair only fires when EVERY cluster on one side conflicts
    with EVERY cluster on the other -- one non-conflicting pair anywhere
    across the two sets is enough to prove the two products are not
    necessarily incompatible, which is what the flag is meant to capture.

    master_title is optional so existing callers (and oneds_competitor) keep
    working unchanged -- without it the master's type comes from master_text
    exactly as before.
    """
    source_clusters = _type_clusters(source_text)
    master_clusters = _master_type_clusters(master_text, master_title)
    if not source_clusters or not master_clusters:
        return 0.5, False
    if source_clusters & master_clusters:
        return 1.0, False
    pairs = [
        (sc, mc) for sc in source_clusters for mc in master_clusters
    ]
    if all(
        (sc, mc) in C.TYPE_HARD_INCOMPAT or (mc, sc) in C.TYPE_HARD_INCOMPAT
        for sc, mc in pairs
    ):
        return 0.0, True
    return 0.3, False


def category_score(source_subcategory: str, master_category: str) -> float:
    """Placeholder substring comparison -- TODO: replace with a cosine
    comparison of embedded subcategory/category text, matching the prior
    art's category_align signal."""
    if not source_subcategory or not master_category:
        return 0.5
    return 1.0 if source_subcategory.lower() in master_category.lower() else 0.3


def text_overlap_score(source_text: str, master_text: str) -> float:
    """Shared-keyword bonus: fraction of source_text's words that appear in master_text."""
    source_words = set(source_text.lower().split())
    master_words = set(master_text.lower().split())
    if not source_words:
        return 0.0
    return len(source_words & master_words) / len(source_words)


def overlap_score(source: SourceProduct, master: MasterProduct) -> float:
    """Shared-keyword bonus for source clean_title."""
    return text_overlap_score(source.clean_title, master.text)


def product_group_match(source_text: str, master: MasterProduct) -> bool:
    """True when the source title names the master's own product group.

    MasterProduct.product_group is the marketing line every size and pack of
    a product shares -- "STRAWBERRY SHINE LIP BALM" covers the 4.5g single,
    the pack of 2 and the pack of 3. Where several candidates are the same
    form and size, the group name in the title is often the only thing that
    separates them (strawberry vs litchi vs peach), and none of the six
    blended signals can see it: the group is not in the master's title text
    verbatim, and token overlap treats "strawberry" as one word among many.

    Matching is token-subset, not substring: every distinctive word of the
    group must appear in the source text. Generic form words are dropped
    first, so a bare "LIP BALM" group cannot claim every lip balm listing --
    a group that reduces to nothing after that is not matchable at all.

    A group that reduces to ONLY generic marketing adjectives ("refreshing",
    "gentle", ...) is a second version of the same problem, one step
    removed: "REFRESHING TONER" reduces to {"refreshing"} alone, which
    passed the subset test against "Refreshing Cleansing Milk" -- a
    different product that happens to share the adjective, not the group.
    But those adjectives are the ONLY thing that separates "GENTLE BABY
    WIPES" from "REFRESHING BABY WIPES" from "SOOTHING BABY WIPES", so they
    cannot be dropped from the token set outright the way a plain
    _GROUP_STOPWORDS entry can -- that regressed 236 rows where the
    adjective is genuinely load-bearing. The fix is narrower: only when the
    reduced set is nothing BUT these adjectives does the match fall back to
    requiring the group's FULL name (adjective and form word both), so
    "gentle baby wipes" still needs both "gentle" and "wipes" in the source
    text, and "refreshing" alone no longer claims an unrelated cleansing
    milk that merely uses the same adjective.
    """
    if not master.product_group:
        return False
    lowered_group = master.product_group.lower()
    tokens = {
        word
        for word in lowered_group.split()
        if word not in _GROUP_STOPWORDS and word not in _GROUP_FILLER
    }
    if not tokens:
        return False
    if tokens <= _GROUP_GENERIC_ADJ:
        # Falling back to the full group name means the form word now has
        # to survive the source tokenisation too -- "shampoo(400" or
        # "soap-" from a listing's own punctuation-glued title never used
        # to matter, because only the adjective needed to match. Strip the
        # same punctuation from the GROUP's tokens as from the source's
        # below, so "GENTLE BABY SHAMPOO" needs "shampoo" to appear, not
        # "shampoo(400" to equal it literally.
        tokens = {
            word.strip(",.|()-")
            for word in lowered_group.split() if word not in _GROUP_FILLER
        }
    source_tokens = {
        word.strip(",.|()-")
        for word in source_text.lower().split()
        if word not in _GROUP_FILLER
    }
    return tokens <= source_tokens


def pack_type_mismatch(source_text: str, master: MasterProduct) -> bool:
    """True when the master is a multipack/kit but the source reads as a
    single unit.

    A twin pack usually carries the SAME PackSize as the single, so
    pack_score() cannot separate them -- this is the only signal that can.
    Deliberately one-directional: a listing that says nothing about count is
    treated as a single, so this fires only when the master is explicitly a
    multi/kit and the title shows no multipack cue. The reverse (source is a
    multipack, master is single) is left alone, since a seller listing a
    bundle of singles is a real and correct mapping.
    """
    pack_type = (master.pack_type or "").upper()
    if not any(marker in pack_type for marker in ("MULTI", "KIT")):
        return False
    # pack_type alone is not trustworthy enough to penalise on. Measured
    # against the master's own titles, it disagrees with them on 1,597 of
    # 3,662 active rows (43.6%): genuine bundles ("PSSL 100ml+PNFW 100ml",
    # "SWTP 175G + TG TB & TC FREE") are filed REGULAR SALES PACK, while
    # plain singles ("SOOTHING BODY LOTION NS 200ml", "NEEM FACE PACK 75gm
    # (RS.20 OFF)") are filed OFFER SALES PK-MULTI.
    #
    # The penalty therefore fired on the wrong row exactly when it mattered:
    # for ACGX4ZWXF2 ("Purifying Neem Face Wash", 200ml) the correct plain
    # 7000861 carries OFFER SALES PK-MULTI and took x0.97, while the bundle
    # 7001609 ("PURIF NEEM FAC WAS 200ml+PNS 50g") carries REGULAR SALES PACK
    # and was spared -- handing rank 1 to the bundle by 0.0136.
    #
    # So require the master's TITLE to corroborate the flag. The title is
    # verifiable; the attribute is not. A row whose title shows no multipack
    # notation at all is not treated as a multipack however it is filed.
    if not _MULTIPACK_CUE.search((master.product_name or "").lower()):
        return False
    return not _MULTIPACK_CUE.search(source_text.lower())


def _domain_mismatch(source_domain: str, master: MasterProduct) -> bool:
    """Cross-domain check (baby vs face)."""
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
    type_align, hard_incompat = type_alignment_score(
        source.title, master.text, master.product_name
    )
    category = category_score(source.subcategory, master.category)
    pack = stage_attributes.pack_score(
        (source.pack_value, source.pack_unit) if source.pack_value is not None else None,
        (master.pack_value, master.pack_unit) if master.pack_value is not None else None,
    )
    
    # Evaluate feature representation overlap scores:
    # 1. Title (clean_title)
    clean_title_ov = text_overlap_score(source.clean_title, master.text)
    
    # 2. Title+ingredient
    ing_text = f"{source.clean_title} {source.ingredient}".strip() if source.ingredient else None
    clean_title_ing_ov = text_overlap_score(ing_text, master.text) if ing_text else None
    
    # 3. Title+Benefit
    ben_text = f"{source.clean_title} {source.benefit}".strip() if source.benefit else None
    clean_title_ben_ov = text_overlap_score(ben_text, master.text) if ben_text else None
    
    # 4. Title+Benefit+ingredient (All)
    all_text = f"{source.clean_title} {source.benefit or ''} {source.ingredient or ''}".strip() if (source.ingredient or source.benefit) else None
    all_ov = text_overlap_score(all_text, master.text) if all_text else None

    # Base blend calculation per representation.
    #
    # The competitor flow keeps its OWN weight vector (W_COMPETITOR_*), which
    # is the only part of this file that is not a straight port of
    # oneds_master.stages.stage_scoring.
    #
    # Two differences from the master vector, both deliberate:
    #   - semantic is 0.30 here against 0.40 there. A competitor listing is
    #     matched against a catalogue it does not belong to, so embedding
    #     similarity is weaker evidence than it is for a Himalaya row.
    #   - category_score still carries W_COMPETITOR_CATEGORY=0.10. The master
    #     blend dropped it and moved that weight onto semantic because the
    #     category signal is applied upstream there, by the category gate.
    #     The competitor flow has no such gate yet, so the in-blend signal is
    #     still the only category evidence it has.
    #
    # Keeping the vectors separate means retuning one flow can never silently
    # shift the other.
    def _blend(ov: float) -> float:
        return (
            C.W_COMPETITOR_SEMANTIC * score_semantic
            + C.W_COMPETITOR_LEXICAL * score_lexical
            + C.W_COMPETITOR_CATEGORY * category
            + C.W_COMPETITOR_TYPE * type_align
            + C.W_COMPETITOR_PACK * pack
            + C.W_COMPETITOR_OVERLAP * ov
        )

    clean_title_score = _blend(clean_title_ov)
    clean_title_ing_score = _blend(clean_title_ing_ov) if clean_title_ing_ov is not None else None
    clean_title_benefit_score = _blend(clean_title_ben_ov) if clean_title_ben_ov is not None else None
    all_score = _blend(all_ov) if all_ov is not None else None

    # Final ensemble selects max score across active feature representations
    valid_scores = [s for s in [clean_title_score, clean_title_ing_score, clean_title_benefit_score, all_score] if s is not None]
    ensemble = max(valid_scores) if valid_scores else clean_title_score

    penalty_applied: str | None = None
    if source.domain != "other" and _domain_mismatch(source.domain, master):
        ensemble *= C.DOMAIN_MISMATCH_PENALTY
        penalty_applied = "domain_mismatch"
    if hard_incompat:
        ensemble *= C.TYPE_HARD_INCOMPAT_PENALTY
        penalty_applied = "type_hard_incompatible"

    # Master-side signals the six blended weights cannot see. Applied after
    # the blend, like the penalties above, so the AHP weight vector stays
    # intact. Both are capped into [0, 1] below.
    group_matched = product_group_match(source.clean_title, master)
    if group_matched:
        ensemble *= C.PRODUCT_GROUP_MATCH_BONUS
        penalty_applied = penalty_applied or "product_group_match"
    if pack_type_mismatch(source.clean_title, master):
        ensemble *= C.PACK_TYPE_MISMATCH_PENALTY
        penalty_applied = penalty_applied or "pack_type_mismatch"

    # Unit count.
    #
    # Silence means ONE, on BOTH sides. A listing that states no count is not
    # making "no claim" -- it is offering a single unit, which is how anyone
    # reads a bare "Himalaya Lip Balm". Treating source silence as unknown
    # and skipping the penalty let bare listings match wholesale cartons:
    # measured, "Himalaya Lip Balm" (no size, no count) on blinkit, swiggy
    # and zepto mapped to LIP BALM 12 X 10 g and LIP BALM 48x10g, three of
    # them at AutoMatch. Those rows have no size either, so pack_score is a
    # flat 0.5 for every candidate and count is the only signal left that
    # can separate a single from a 12-pack.
    #
    # Suppressed when the product group matches. Count is a packaging fact;
    # the group is product identity, and identity has to win. Measured
    # without this guard, a Strawberry Shine "Pack of 2" listing left the
    # correct STRAWBERRY (Pack of 3) row for a CHERRY (Pack of 2) one --
    # trading the right product for the right carton.
    if not group_matched:
        source_count = source.pack_count if source.pack_count is not None else 1
        master_count = master.pack_count if master.pack_count is not None else 1

        if source_count != master_count:
            # Scaled by agreement, so 2-vs-3 is nudged and 1-vs-48 is pushed
            # hard. Scoring silence as 1 rather than as "one out" matters for
            # larger packs: a distance-scaled proxy leaves 4-vs-silent at
            # 0.97, which still beats a genuine 4-vs-2 at 0.925.
            count_agreement = stage_attributes.pack_count_score(
                source_count, master_count
            )
            ensemble *= 1.0 - (1.0 - C.PACK_COUNT_MISMATCH_PENALTY) * (
                1.0 - count_agreement
            )
            penalty_applied = penalty_applied or "pack_count_mismatch"

    # The group bonus is the only multiplier above 1.0, so clamp -- every
    # downstream threshold (TIER_HIGH, determine_mapping_status) assumes a
    # 0-1 ensemble, and an unclamped 1.08x would let a strong match cross
    # TIER_HIGH on the bonus alone.
    ensemble = min(1.0, max(0.0, ensemble))

    return ScoreBreakdown(
        semantic=score_semantic, lexical=score_lexical, category=category,
        type_align=type_align, pack=pack, overlap=clean_title_ov, ensemble=ensemble,
        penalty_applied=penalty_applied,
        clean_title_score=clean_title_score,
        clean_title_ing_score=clean_title_ing_score,
        clean_title_benefit_score=clean_title_benefit_score,
        all_score=all_score,
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
    # COMPETITOR_TOP_N_OUTPUT, not TOP_N_OUTPUT: the two flows size their
    # shortlist independently, so tuning the master judge's width does not
    # silently change what a competitor row sends or persists.
    return scored[: C.COMPETITOR_TOP_N_OUTPUT]
