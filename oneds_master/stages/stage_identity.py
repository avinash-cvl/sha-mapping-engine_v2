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


# How many distinct product groups each token appears in, learned from the
# master rather than listed by hand. Populated by build_token_frequency()
# once per run; empty means "unknown", and the rarity rule then does nothing
# rather than guessing.
_TOKEN_GROUP_FREQUENCY: dict[str, int] = {}


def build_token_frequency(master_rows) -> None:
    """Count how many product groups each token appears in.

    This is what separates a variant name from a filler word without anyone
    maintaining a list. Measured on the live master: "shine" appears in 7
    groups, "peach" and "cherry" in 2 each -- so a listing that shares only
    "shine" with the CHERRY group has matched nothing that identifies a
    product. 224 tokens are unique to a single group; 11 appear in 16 or more.

    Called once per run from step_3, after the master is loaded. Cheap: one
    pass over ~650 distinct groups.
    """
    frequency: dict[str, int] = {}
    seen_groups: set[str] = set()
    for row in master_rows:
        group = getattr(row, "product_group", "") or ""
        if not group or group in seen_groups:
            continue
        seen_groups.add(group)
        for word in _tokens(group):
            if (word in stage_scoring._GROUP_STOPWORDS
                    or word in stage_scoring._GROUP_FILLER
                    or word in C.IDENTITY_CATALOGUE_WORDS):
                continue
            frequency[word] = frequency.get(word, 0) + 1
    _TOKEN_GROUP_FREQUENCY.clear()
    _TOKEN_GROUP_FREQUENCY.update(frequency)


def _rarest_frequency(words: set[str]) -> int:
    """Group-count of the rarest token in `words`.

    A large number when frequencies are unknown, so an unpopulated table
    leaves the rarity rule inert rather than treating every missing token as
    a variant.
    """
    if not _TOKEN_GROUP_FREQUENCY:
        return 10 ** 6
    return min(_TOKEN_GROUP_FREQUENCY.get(word, 10 ** 6) for word in words)


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

    # How many of the five identity criteria could actually be checked.
    # 1.0 means brand, family, form, size AND count were all comparable;
    # 0.8 means one of them was unstated on one side and simply unknown.
    # Distinct from attribute_score, which measures how well the COMPARABLE
    # ones agreed -- a pair can agree perfectly on four criteria and still
    # have said nothing about the fifth.
    coverage: float = 1.0

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
        if self.identity_match and self.coverage < 1.0:
            parts.append(f"PARTIAL(cov={self.coverage:.0%})")
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
    """True/False when the counts are comparable, None when they are not.

    Silence on BOTH sides is None -- neither states a count, so there is
    nothing to compare and treating that as agreement would manufacture
    identity out of a gap in the catalogue.

    But silence on ONE side, when the other states a multipack, is NOT
    unknown. A listing that says "Pack of 5" against a master that says
    nothing is a 5-pack against a single unit: the catalogue names its
    multipacks explicitly ("PACK OF 2", "24x10g", "3X60'S"), so a master row
    that stays silent is a single, and that is a real disagreement.

    Measured on live data, treating it as unknown inverted a whole ranking:
    "Litchi Shine Lip Care 4.5G (Pack Of 5)" scored a perfect 1.00 against
    both LITCHI SHINE LIP CARE 4.5G (a single) and LITCHI SHINE SERUM 4.5G,
    because neither states a count so count was excluded -- while the one
    candidate that DOES declare a count, PACK OF 2, was capped at 0.60 for
    declaring the wrong one. Silence outscored honesty.

    A source that states nothing while the master states a multipack stays
    None. Marketplace titles omit pack information constantly, so that
    direction really is unknown rather than a claim about a single.
    """
    if source_count is None and master_count is None:
        return None
    if source_count is not None and master_count is None:
        # Listing declares a multipack, catalogue row does not: single unit.
        return False if source_count > 1 else None
    if source_count is None:
        # Mirror of the case above. A listing that names no pack is offering
        # one unit -- that is how anyone reads "Himalaya Peach Shine Lip Care,
        # 4.5g" -- so a master row declaring PACK OF 3 is a different sellable
        # unit, not an unknown.
        #
        # Handling only the other direction left this wide open: the single,
        # the PACK OF 2 and the PACK OF 3 all scored a perfect 1.00 against
        # one silent listing, because count was dropped as unstated and every
        # remaining attribute agreed. Three different products, three
        # identical scores.
        return False if master_count > 1 else None
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
    missing = distinctive - source_words

    # A token the listing does NOT have, which is rare across the catalogue,
    # is a variant name -- and a missing variant name is a different product.
    #
    # Plain overlap treats every token alike, which is how "Peach Shine Lip
    # Care" matched the CHERRY SHINE LIP BALM group: the two groups reduce to
    # {peach, shine} and {cherry, shine}, the listing shares "shine", and
    # 1/2 = 0.50 cleared the match ratio. The one token that actually names
    # the product -- peach vs cherry -- was worth exactly as much as the one
    # that names nothing. Both scored 100%.
    #
    # Rarity is measured from the master itself rather than listed by hand:
    # "shine" is in 7 groups, "peach" and "cherry" in 2 each. A token in few
    # groups identifies a line; one in many does not.
    if missing and _rarest_frequency(missing) <= C.IDENTITY_VARIANT_MAX_GROUPS:
        return False

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
        coverage=round(len(comparable) / len(checks), 4) if checks else 0.0,
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
    coverage: float = 1.0,
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
        # Top score only when EVERY criterion was actually checked.
        #
        # identity_match means "nothing contradicted and enough agreed", which
        # is not the same as "all five verified". Measured on lip makeup, 63
        # of 77 top-scoring rows had verified only four -- and the unverified
        # one was usually count, which is exactly what separates a single from
        # a multipack. Worse, "Litchi Shine Lip Care 4.5g" scored 0.99 against
        # both LITCHI SHINE LIP CARE and LITCHI SHINE SERUM LIP BALM: same
        # four criteria agreed, and the word that distinguishes them (serum)
        # is not one of the five.
        #
        # So a partial verification is scaled down by how much of the evidence
        # was actually available. Four of five is strong, but it is not the
        # same claim as five of five, and the number a reviewer sees should
        # say so.
        if coverage >= 1.0:
            return C.IDENTITY_MAX_SCORE
        return round(C.IDENTITY_MAX_SCORE * (
            C.IDENTITY_PARTIAL_FLOOR
            + (1.0 - C.IDENTITY_PARTIAL_FLOOR) * coverage
        ), 4)
    if critical_conflict:
        # Scale, then ceiling. A flat min(blended, cap) made every conflicted
        # candidate render as the identical number -- measured, ten rows with
        # ensembles from 0.89 to 0.73 all displayed 0.60, so the portal showed
        # no difference between a near-miss on pack count and a poor match
        # that also had one. The multiplier keeps them ordered; the ceiling
        # keeps all of them below the AutoMatch line.
        return max(0.0, min(blended * C.IDENTITY_CONFLICT_PENALTY, C.IDENTITY_CONFLICT_CAP))
    # Capped at IDENTITY_MAX_SCORE too, so no path reaches 1.0 -- a blend of
    # eight signals that all happen to be perfect is still not certainty.
    return max(0.0, min(C.IDENTITY_MAX_SCORE, blended))
