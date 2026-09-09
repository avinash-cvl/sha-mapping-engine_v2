"""Product identity -- is this the SAME sellable unit, not just a similar row.

The ensemble in stage_scoring.py answers "how similar is this pair" from six
text/vector signals. That is a different question from "is this the same thing
a shopper would receive", and the gap between them is measurable: the master's

    TAN REMOVAL ORANGE PEEL OFF MASK 8G 1X12N SACHET

against the listing

    Himalaya Tan Removal Orange Peel Off Mask, 8gm, Pack of 12

scored an ensemble of 0.23 -- BELOW a Dark Spot Turmeric face pack at 0.55 --
even though the two are the same product. Nothing was wrong with retrieval;
the correct row was rank 1 of the candidate list. The similarity signals
simply cannot see that "8gm, Pack of 12" and "8G 1X12N" describe one pack,
because they share almost no literal tokens.

This module compares PARSED ATTRIBUTES instead, so notation stops mattering.
It answers two things:

    identity_match     -- do the identity attributes agree well enough that
                          this is the same sellable unit?
    critical_conflict  -- is there a fact that rules the pair out regardless
                          of how confident anything else is?

The second exists because the first must not be a one-way ratchet. A judge
that is 95% sure about a 100ml listing against a 500ml master is 95% sure of
something false, and confidence is not evidence. A conflict is a fact about
the products; it caps the score whatever the model says.

Everything here is derived from parsed values and existing vocabulary --
config.TYPE_CLUSTERS, config.HIMALAYA_BRANDS, the master's own
normalized_product_group. No SKU, product code or title is special-cased.

Reuses rather than reimplements: stage_attributes.parse_pack /
parse_pack_count / normalize_pack for the pack facts, and
stage_scoring._type_cluster for product form.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import common.config as C
from common.models import MasterProduct, SourceProduct
from oneds_master.stages import stage_attributes, stage_scoring

# Splits on anything that is not a letter or digit, so punctuation and
# hyphenation stop being differences.
#
# This is what broke the very case the module exists for. The master writes
# its group as "TAN REMOVAL ORANGE MASK (PEEL-OFF)" and the listing writes
# "Tan Removal Orange Peel Off Mask": whitespace-splitting yields the token
# "(peel-off)" on one side and "peel", "off" on the other, so the family check
# reported a mismatch on two spellings of one word. Catalogue text and
# marketplace text disagree about punctuation constantly -- "(PEEL-OFF)",
# "PEEL-OFF", "peel off" -- and none of that is product identity.
_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    """Lower-cased alphanumeric tokens, punctuation and hyphens removed."""
    return set(_WORD_RE.findall((text or "").lower()))


@dataclass(frozen=True)
class IdentityVerdict:
    """The identity layer's answer for one (source, candidate) pair.

    `comparable` names the attributes that could actually be judged. An
    attribute neither side states is not evidence of agreement OR of conflict,
    and is excluded from the score rather than counted as either -- otherwise
    a sparse master row would look like a perfect match on silence alone.
    """

    identity_match: bool
    attribute_score: float
    critical_conflict: bool
    conflicts: tuple[str, ...] = ()
    agreed: tuple[str, ...] = ()
    comparable: tuple[str, ...] = ()
    evidence: dict[str, object] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        """One-line trace, e.g. 'identity attr=0.92 agree=family,size,count'.

        Records WHY identity was denied when the score alone would have
        allowed it -- 'attr=1.00' with no identity is otherwise baffling to
        anyone reading the row, and thin-evidence denials are the common case.
        """
        head = "identity" if self.identity_match else "no-identity"
        parts = [f"{head} attr={self.attribute_score:.2f}"]
        if self.agreed:
            parts.append("agree=" + ",".join(self.agreed))
        if self.conflicts:
            parts.append("CONFLICT=" + ",".join(self.conflicts))
        if not self.identity_match and not self.conflicts:
            discriminating = [n for n in self.agreed if n in C.IDENTITY_DISCRIMINATING]
            if (len(self.comparable) < C.IDENTITY_MIN_COMPARABLE
                    or len(discriminating) < C.IDENTITY_MIN_DISCRIMINATING):
                parts.append(
                    f"THIN(cmp={len(self.comparable)},disc={len(discriminating)})"
                )
        return " ".join(parts)


def _source_pack(source: SourceProduct) -> tuple[float, str] | None:
    """Pack size for the listing, preferring its structured columns.

    extract_attributes() has usually already folded pack_value/pack_unit into
    grams/millilitres, so that is the trustworthy value. The title is the
    fallback for rows whose columns are empty -- which is common, and is
    exactly where the notation problem lives.
    """
    if source.pack_value is not None and source.pack_unit:
        normalised = stage_attributes.normalize_pack(source.pack_value, source.pack_unit)
        if normalised is not None:
            return normalised
    return stage_attributes.parse_pack(source.title or "")


def _master_pack(master: MasterProduct) -> tuple[float, str] | None:
    if master.pack_value is not None and master.pack_unit:
        normalised = stage_attributes.normalize_pack(master.pack_value, master.pack_unit)
        if normalised is not None:
            return normalised
    return stage_attributes.parse_pack(master.product_name or master.text or "")


def _source_count(source: SourceProduct) -> int | None:
    if source.pack_count is not None:
        return source.pack_count
    return stage_attributes.parse_pack_count(source.title or "")


def _master_count(master: MasterProduct) -> int | None:
    if master.pack_count is not None:
        return master.pack_count
    return stage_attributes.parse_pack_count(master.product_name or master.text or "")


def sizes_agree(
    source_pack: tuple[float, str] | None,
    master_pack: tuple[float, str] | None,
    tolerance: float | None = None,
) -> bool | None:
    """True/False when both sides state a size, None when either is silent.

    Compares normalised magnitudes, so 1 KG and 1000 GM agree while 100 ML and
    500 ML do not. The tolerance absorbs 4.5 vs 4.50 and rounding in the
    source columns; it is far too tight to let two genuinely different sizes
    through.
    """
    if source_pack is None or master_pack is None:
        return None
    (source_value, source_unit), (master_value, master_unit) = source_pack, master_pack
    if source_unit != master_unit:
        # Grams against millilitres is not a mismatch, it is an unanswerable
        # question -- the two are not on one scale. Treat as unstated.
        return None
    if max(source_value, master_value) <= 0:
        return None
    tol = C.IDENTITY_PACK_TOLERANCE if tolerance is None else tolerance
    return abs(source_value - master_value) / max(source_value, master_value) <= tol


def counts_agree(source_count: int | None, master_count: int | None) -> bool | None:
    """True/False when both sides state a count, None when either is silent.

    Silence stays None here, unlike pack_count_score() which reads it as one.
    That function is scoring a similarity; this one is asserting an identity,
    and asserting "the master says nothing, therefore it is a single" would
    manufacture a conflict out of a gap in the catalogue.
    """
    if source_count is None or master_count is None:
        return None
    return source_count == master_count


def forms_agree(source_text: str, master_text: str) -> bool | None:
    """Product form agreement via the existing TYPE_CLUSTERS vocabulary.

    None when either side has no recognisable form word -- the vocabulary is
    finite and a miss means "unknown", not "different".
    """
    source_cluster = stage_scoring._type_cluster(source_text or "")
    master_cluster = stage_scoring._type_cluster(master_text or "")
    if source_cluster is None or master_cluster is None:
        return None
    return source_cluster == master_cluster


def brands_agree(source: SourceProduct, master: MasterProduct) -> bool | None:
    """Both sides Himalaya, per the configured brand list.

    The master catalogue is Himalaya-only, so this is nearly always True and
    contributes little -- it is here so that a source row from another brand
    cannot quietly claim identity against a Himalaya master.
    """
    brands = [brand.strip().lower() for brand in C.HIMALAYA_BRANDS if brand.strip()]
    if not brands:
        return None
    source_brand = (source.brand or "").strip().lower()
    if not source_brand:
        return None
    return any(brand in source_brand for brand in brands)


def families_agree(source: SourceProduct, master: MasterProduct) -> bool | None:
    """Does the listing name the master's own product line?

    product_group is the master's marketing grouping -- every size and pack of
    one line shares it -- so it is the closest thing the catalogue has to a
    product family. Reuses stage_scoring.product_group_match(), which strips
    generic form words first so a group that reduces to "LIP BALM" cannot
    claim every lip balm listing.

    None when the master states no group, or when its group is entirely
    generic and therefore unmatchable.
    """
    if not master.product_group:
        return None
    distinctive = {
        word
        for word in _tokens(master.product_group)
        if word not in stage_scoring._GROUP_STOPWORDS
        and word not in stage_scoring._GROUP_FILLER
        and word not in C.IDENTITY_CATALOGUE_WORDS
    }
    if not distinctive:
        return None
    # Punctuation-insensitive on BOTH sides -- product_group_match() splits on
    # whitespace, which is right for the scoring bonus it feeds but leaves
    # "(PEEL-OFF)" unmatchable against "peel off" here.
    source_words = {
        word
        for word in _tokens(source.clean_title or source.title or "")
        if word not in stage_scoring._GROUP_FILLER
    }

    # Proportional, not all-or-nothing. Exact subset was the first
    # implementation and it was wrong in a way that matters: a marketplace
    # title routinely drops a catalogue qualifier, so "Himalaya Neem Face Wash
    # 100ml" against the group "PURIFYING NEEM FACE WASH" lost on the single
    # missing word "purifying" and was declared a different product family --
    # which then fired a critical conflict and buried a genuine match. An
    # omitted adjective is not a claim about a different line.
    #
    # A DIFFERENT line looks nothing like this: "TAN REMOVAL ORANGE" against
    # "DARK SPOT CLEARING TURMERIC" shares no distinctive token at all. That is
    # the case the conflict is for, and overlap separates the two cleanly --
    # near-1.0 for a dropped qualifier, near-0.0 for a different product.
    #
    # None in the middle band: partial overlap is genuinely ambiguous, and
    # "unknown" is the honest answer. It leaves the attribute out of the score
    # rather than inventing agreement or a conflict.
    shared = distinctive & source_words
    overlap = len(shared) / len(distinctive)

    # A ratio alone cannot judge a short group. 251 of the master's 653 groups
    # reduce to exactly TWO distinctive tokens, so dropping a single qualifier
    # scores precisely 0.50 -- permanently ambiguous, for a third of the
    # catalogue. "PURIFYING NEEM FACE WASH" against a listing saying "Neem Face
    # Wash" is a dropped adjective, not an unanswerable question.
    #
    # So agreement is either proportional OR absolute: enough of the group
    # named, or enough distinct tokens named outright. The absolute floor is
    # what rescues short groups; the ratio is what keeps a long group from
    # matching on two words out of six.
    if overlap >= C.IDENTITY_FAMILY_MATCH_RATIO:
        return True
    if len(shared) >= C.IDENTITY_FAMILY_MIN_SHARED and overlap > C.IDENTITY_FAMILY_CONFLICT_RATIO:
        return True
    if overlap <= C.IDENTITY_FAMILY_CONFLICT_RATIO:
        return False
    return None


def evaluate(source: SourceProduct, master: MasterProduct) -> IdentityVerdict:
    """Compare one (source, candidate) pair on identity attributes.

    attribute_score is the configured-weight average over the attributes that
    were actually comparable, renormalised so silence neither helps nor hurts.
    A pair with nothing comparable scores 0.0 and is never an identity match --
    absence of evidence is not evidence.
    """
    source_text = source.clean_title or source.title or ""
    master_text = master.product_name or master.text or ""

    source_pack, master_pack = _source_pack(source), _master_pack(master)
    source_count, master_count = _source_count(source), _master_count(master)

    checks: list[tuple[str, bool | None, float]] = [
        ("brand", brands_agree(source, master), C.IDENTITY_W_BRAND),
        ("family", families_agree(source, master), C.IDENTITY_W_FAMILY),
        ("form", forms_agree(source_text, master_text), C.IDENTITY_W_FORM),
        ("size", sizes_agree(source_pack, master_pack), C.IDENTITY_W_SIZE),
        ("count", counts_agree(source_count, master_count), C.IDENTITY_W_COUNT),
    ]

    agreed: list[str] = []
    disagreed: list[str] = []
    comparable: list[str] = []
    earned = 0.0
    available = 0.0

    for name, verdict, weight in checks:
        if verdict is None:
            continue
        comparable.append(name)
        available += weight
        if verdict:
            agreed.append(name)
            earned += weight
        else:
            disagreed.append(name)

    attribute_score = (earned / available) if available > 0 else 0.0

    # Critical conflicts. Each is a positively-established disagreement, never
    # an absence: a size that neither side states cannot conflict.
    conflicts: list[str] = []
    if C.IDENTITY_CONFLICT_ON_SIZE and "size" in disagreed:
        conflicts.append("pack_size")
    if C.IDENTITY_CONFLICT_ON_COUNT and "count" in disagreed:
        conflicts.append("pack_count")
    if C.IDENTITY_CONFLICT_ON_FORM and "form" in disagreed:
        conflicts.append("product_form")
    if C.IDENTITY_CONFLICT_ON_BRAND and "brand" in disagreed:
        conflicts.append("brand")
    # Family is the ONLY attribute that separates two different products of
    # the same form and pack. Measured: a Tan Removal 8g x12 mask and a Dark
    # Spot Turmeric 8g x12 mask agree on brand, form, size AND count -- every
    # other check passes, and without this the pair scores 0.70 and looks like
    # near-identity. A stated, positively-different product line is a fact
    # about the products, which is what makes it critical rather than merely
    # low-scoring.
    if C.IDENTITY_CONFLICT_ON_FAMILY and "family" in disagreed:
        conflicts.append("product_family")

    critical_conflict = bool(conflicts)

    # Identity requires DISCRIMINATING agreement, not merely a high ratio.
    #
    # attribute_score renormalises over whatever was comparable, so a listing
    # that states nothing beyond its brand and form scores a perfect 1.00 --
    # it agreed on 100% of the little there was to agree about. Measured
    # against steward verdicts on hair-care shampoos, that alone promoted 8 of
    # 9 known-WRONG matches to a deterministic 100%: "himalaya anti dandruff
    # shampoo" (no size stated) was declared identical to a specific 400ml
    # master purely on brand+form.
    #
    # Brand is near-constant here and form is coarse, so neither identifies a
    # product. The gates below demand a floor of comparable evidence AND that
    # enough of it comes from attributes that actually discriminate -- family,
    # size, count. A high ratio over thin evidence is not identity, it is a
    # small sample.
    discriminating_agreed = [name for name in agreed if name in C.IDENTITY_DISCRIMINATING]

    identity_match = (
        not critical_conflict
        and len(comparable) >= C.IDENTITY_MIN_COMPARABLE
        and len(discriminating_agreed) >= C.IDENTITY_MIN_DISCRIMINATING
        and attribute_score >= C.IDENTITY_MATCH_THRESHOLD
    )

    return IdentityVerdict(
        identity_match=identity_match,
        attribute_score=round(attribute_score, 4),
        critical_conflict=critical_conflict,
        conflicts=tuple(conflicts),
        agreed=tuple(agreed),
        comparable=tuple(comparable),
        evidence={
            "source_pack": source_pack,
            "master_pack": master_pack,
            "source_count": source_count,
            "master_count": master_count,
            "disagreed": tuple(disagreed),
        },
    )


def final_score(
    scores,
    attribute_score: float,
    llm_confidence: float | None,
    identity_match: bool,
    critical_conflict: bool,
) -> float:
    """Blend the eight signals into one score, then apply the two overrides.

        base = sum(W2_x * signal_x)  over the six ensemble signals
                                     plus attribute_score and the LLM

    then:
        identity_match and no conflict -> 1.0   (the pair IS the sellable unit)
        critical_conflict              -> capped at IDENTITY_CONFLICT_CAP

    The cap is applied last and unconditionally, so no combination of
    confident signals can carry a pair past a fact that rules it out.

    llm_confidence of None/NaN means the judge did not run. Its weight is then
    redistributed across the remaining signals rather than scored as zero,
    which would silently penalise every row the judge was never asked about.
    """
    llm_ran = llm_confidence is not None and llm_confidence == llm_confidence

    terms = [
        (C.W2_SEMANTIC, scores.semantic),
        (C.W2_LEXICAL, scores.lexical),
        (C.W2_CATEGORY, scores.category),
        (C.W2_TYPE, scores.type_align),
        (C.W2_PACK, scores.pack),
        (C.W2_OVERLAP, scores.overlap),
        (C.W2_ATTRIBUTES, attribute_score),
    ]
    if llm_ran:
        terms.append((C.W2_LLM, float(llm_confidence)))

    total_weight = sum(weight for weight, _ in terms)
    if total_weight <= 0:
        return 0.0

    blended = sum(weight * value for weight, value in terms) / total_weight

    if identity_match and not critical_conflict:
        return 1.0
    if critical_conflict:
        return min(blended, C.IDENTITY_CONFLICT_CAP)
    return max(0.0, min(1.0, blended))
