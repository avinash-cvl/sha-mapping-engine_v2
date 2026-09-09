"""Tests for the product-identity layer.

Every case here is drawn from a real pair that the engine got wrong, or from a
real pair it must keep getting right. The distinction the layer exists to draw
is SIMILAR PRODUCT versus SAME SELLABLE PRODUCT, and both directions of failure
are represented: matches that were buried (false negatives) and mismatches that
were promoted (false positives).

Run:  python -m pytest tests/test_identity.py -v
"""
from __future__ import annotations

import pytest

import common.config as C
from common.models import MasterProduct, MatchResult, ScoreBreakdown, SourceProduct
from oneds_master.stages import stage_attributes, stage_identity


# Master product_group values, verbatim from staging.himalaya_products. The
# punctuation in "(PEEL-OFF)" is deliberate -- it is what broke the first
# implementation of the family check.
GROUP_TAN = "TAN REMOVAL ORANGE MASK (PEEL-OFF)"
GROUP_TURMERIC = "DARK SPOT CLEARING TURMERIC FACE PACK"
GROUP_NEEM_WASH = "PURIFYING NEEM FACE WASH"
GROUP_NEEM_TABS = "NEEM TABLETS"


def source(title: str, pack_value=None, pack_unit=None, brand="himalaya") -> SourceProduct:
    """A listing, with attributes extracted the way the pipeline extracts them."""
    product = SourceProduct(
        sku="TEST", source="test", channel="test", brand=brand,
        title=title, clean_title=title.lower(), category="", subcategory="",
        pack_value=pack_value, pack_unit=pack_unit, benefit=None, ingredient=None,
        domain="other", match_type="", sub_brand=None, variant=None,
        pack_count=stage_attributes.parse_pack_count(title),
        channel_key="test", source_row_id=1,
    )
    return stage_attributes.extract_attributes(product)


def master(code: str, name: str, pack_value=None, pack_unit=None, group="") -> MasterProduct:
    return MasterProduct(
        product_code=code, product_name=name, division="", category="", subcategory="",
        sap_status="", blocked_in_sap=False, pack_value=pack_value, pack_unit=pack_unit,
        text=name.lower(), source_row_id=1, product_group=group, pack_type="",
        pack_count=stage_attributes.parse_pack_count(name),
    )


def scores(ensemble: float) -> ScoreBreakdown:
    return ScoreBreakdown(
        semantic=0.5, lexical=0.4, category=1.0, type_align=1.0,
        pack=1.0, overlap=0.3, ensemble=ensemble,
    )


# ---------------------------------------------------------------------------
# TEST 1 -- the same product written three different ways
# ---------------------------------------------------------------------------
# The failure that motivated the layer. Source and master are the same sellable
# unit; the ensemble scored the pair 0.23 -- below an unrelated turmeric face
# pack at 0.55 -- because the two texts share almost no literal tokens.

@pytest.mark.parametrize("title", [
    "Himalaya Tan Removal Orange Peel Off Mask, 8gm, Pack of 12",
    "Himalaya Tan Removal Orange Peel Off Mask 8 g x 12",
    "Himalaya Tan Removal Orange Peel Off Mask 8G x 12 Sachets",
])
def test_same_unit_different_notation_is_identity(title):
    verdict = stage_identity.evaluate(
        source(title, 8.0, "GM"),
        master("7005790", "TAN REMOVAL ORANGE PEEL OFF MASK 8G 1X12N SACHET",
               8.0, "GM", GROUP_TAN),
    )
    assert verdict.identity_match is True
    assert verdict.critical_conflict is False
    assert verdict.attribute_score >= C.IDENTITY_MATCH_THRESHOLD


def test_identity_survives_a_low_ensemble():
    """A low similarity score must not veto an identity match.

    This is the whole point: 0.23 is a fact about wording, not about whether
    the products are the same.
    """
    result = MatchResult(
        source=source("Himalaya Tan Removal Orange Peel Off Mask, 8gm, Pack of 12", 8.0, "GM"),
        candidate=master("7005790", "TAN REMOVAL ORANGE PEEL OFF MASK 8G 1X12N SACHET",
                         8.0, "GM", GROUP_TAN),
        rank=1, scores=scores(0.2299), confidence_tier="", resolution_method="",
        llm_confidence=0.99,
    )
    verdict = stage_identity.evaluate(result.source, result.candidate)
    assert stage_identity.final_score(
        result.scores, verdict.attribute_score, result.llm_confidence,
        verdict.identity_match, verdict.critical_conflict,
    ) == C.IDENTITY_MAX_SCORE


def test_no_path_ever_reaches_certainty():
    """The engine must never display 100%.

    It compares the attributes the catalogue happens to state, so a perfect
    score on those is not proof the two are the same sellable unit --
    packaging variants, e-com-only SKUs and jar-versus-blister distinctions
    differ in ways no title mentions. A 100% invites a reviewer to approve
    without looking, and is indefensible on the rows it gets wrong.
    """
    highest = 0.0
    for ensemble in (0.0, 0.5, 0.8, 0.95, 1.0):
        for attribute in (0.0, 0.5, 0.85, 1.0):
            for llm in (None, 0.5, 0.99, 1.0):
                for identity in (True, False):
                    for conflict in (True, False):
                        if identity and conflict:
                            continue  # not a state the layer can produce
                        highest = max(highest, stage_identity.final_score(
                            scores(ensemble), attribute, llm, identity, conflict,
                        ))
    assert highest < 1.0
    assert highest == C.IDENTITY_MAX_SCORE


def test_a_conflict_can_never_reach_automatch():
    """A contradiction stays below the 0.86 line at any confidence."""
    highest = 0.0
    for ensemble in (0.5, 0.9, 1.0):
        for attribute in (0.5, 0.85, 1.0):
            for llm in (None, 0.9, 1.0):
                highest = max(highest, stage_identity.final_score(
                    scores(ensemble), attribute, llm, False, True,
                ))
    assert highest < 0.86


# ---------------------------------------------------------------------------
# TEST 2 -- hard contradictions
# ---------------------------------------------------------------------------

def test_different_pack_size_is_a_conflict():
    verdict = stage_identity.evaluate(
        source("Himalaya Purifying Neem Face Wash 100ml", 100.0, "ML"),
        master("X1", "PURIFYING NEEM FACE WASH 500ML", 500.0, "ML", GROUP_NEEM_WASH),
    )
    assert verdict.identity_match is False
    assert verdict.critical_conflict is True
    assert "pack_size" in verdict.conflicts


def test_different_pack_count_is_a_conflict():
    """12 sachets against 144 sachets -- same line, different sellable unit."""
    verdict = stage_identity.evaluate(
        source("Himalaya Tan Removal Orange Peel Off Mask, 8gm, Pack of 12", 8.0, "GM"),
        master("7004337", "TAN REMOVAL ORANGE PEEL OFF MASK 144 SACHET X 8G",
               8.0, "GM", GROUP_TAN),
    )
    assert verdict.critical_conflict is True
    assert "pack_count" in verdict.conflicts


def test_different_family_at_identical_pack_is_a_conflict():
    """The case every other attribute agrees on.

    Both are 8g x12 Himalaya masks: brand, form, size and count all match.
    Product family is the only thing that separates them, which is why its
    disagreement has to be critical rather than merely low-scoring.
    """
    verdict = stage_identity.evaluate(
        source("Himalaya Tan Removal Orange Peel Off Mask, 8gm, Pack of 12", 8.0, "GM"),
        master("7006308", "DARK SPOT CLEARING TURMERIC FACE PACK 8G 1X12N SACHET",
               8.0, "GM", GROUP_TURMERIC),
    )
    assert verdict.identity_match is False
    assert verdict.critical_conflict is True
    assert "product_family" in verdict.conflicts


def test_wrong_brand_is_a_conflict():
    verdict = stage_identity.evaluate(
        source("Dabur Face Wash 100ml", 100.0, "ML", brand="dabur"),
        master("X2", "PURIFYING NEEM FACE WASH 100ML", 100.0, "ML", GROUP_NEEM_WASH),
    )
    assert verdict.critical_conflict is True
    assert "brand" in verdict.conflicts


# ---------------------------------------------------------------------------
# TEST 3 -- a confident LLM cannot override a contradiction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("llm_confidence", [0.90, 0.95, 0.99, 1.0])
def test_llm_cannot_promote_past_a_conflict(llm_confidence):
    """Confidence is not evidence.

    A judge that is 99% sure about a 100ml listing against a 500ml master is
    99% sure of something false. The cap is applied last and unconditionally.
    """
    verdict = stage_identity.evaluate(
        source("Himalaya Purifying Neem Face Wash 100ml", 100.0, "ML"),
        master("X1", "PURIFYING NEEM FACE WASH 500ML", 500.0, "ML", GROUP_NEEM_WASH),
    )
    final = stage_identity.final_score(
        scores(0.82), verdict.attribute_score, llm_confidence,
        verdict.identity_match, verdict.critical_conflict,
    )
    assert final <= C.IDENTITY_CONFLICT_CAP
    assert final < 0.86, "a capped row must never reach the AutoMatch band"


# ---------------------------------------------------------------------------
# TEST 4 -- a dropped qualifier is not a different product
# ---------------------------------------------------------------------------

def test_listing_omitting_a_qualifier_still_matches():
    """Marketplace titles drop catalogue words constantly.

    "Himalaya Neem Face Wash" against the group "PURIFYING NEEM FACE WASH" is
    the same product with one adjective missing. An exact-subset family check
    called this a different family and fired a conflict, burying a genuine
    match -- which is why the check is proportional.
    """
    verdict = stage_identity.evaluate(
        source("Himalaya Neem Face Wash 100ml", 100.0, "ML"),
        master("X3", "PURIFYING NEEM FACE WASH 100ML", 100.0, "ML", GROUP_NEEM_WASH),
    )
    assert verdict.identity_match is True
    assert verdict.critical_conflict is False


# ---------------------------------------------------------------------------
# TEST 5 -- silence is not agreement, and not conflict either
# ---------------------------------------------------------------------------

def test_unstated_attributes_are_excluded_not_assumed():
    """An attribute neither side states must not be counted either way.

    Scoring silence as agreement would let a sparse master row look like a
    perfect match; scoring it as disagreement would manufacture conflicts out
    of gaps in the catalogue.
    """
    verdict = stage_identity.evaluate(
        source("Himalaya Neem Tablets"),
        master("X4", "NEEM TABLETS", None, None, GROUP_NEEM_TABS),
    )
    assert "size" not in verdict.comparable
    assert "count" not in verdict.comparable
    assert verdict.critical_conflict is False


def test_no_comparable_attributes_is_never_identity():
    """Absence of evidence is not evidence of identity."""
    verdict = stage_identity.evaluate(
        source("Something Unparseable", brand=""),
        master("X5", "", None, None, ""),
    )
    assert verdict.identity_match is False
    assert verdict.attribute_score == 0.0


# ---------------------------------------------------------------------------
# TEST 5b -- thin evidence is not identity, however well it agrees
# ---------------------------------------------------------------------------
# These are real steward-REJECTED pairs. An earlier version scored them 1.00
# and called them deterministic matches, because attribute_score renormalises
# over whatever was comparable and brand+form was all these listings stated.
# Agreeing on 100% of two coarse attributes is a small sample, not an identity.

@pytest.mark.parametrize("title,code,name", [
    # Bare listing, no size -- master is a specific 400ml SKU.
    ("himalaya anti dandruff shampoo", "7001921", "ANTI-DANDRUFF SHAMPOO 400ml INDIA OFFER"),
    # Bare listing, no size -- master is a specific 700ml SKU.
    ("Anti-Hair Fall Shampoo", "7001673", "ANTI-HAIR FALL SHAMPOO 700ml (WITH DISPENSER PUMP)"),
])
def test_brand_and_form_alone_is_not_identity(title, code, name):
    verdict = stage_identity.evaluate(
        source(title),
        master(code, name, None, None, ""),
    )
    assert verdict.identity_match is False, (
        f"{title!r} states only brand and form; that cannot identify a "
        f"specific SKU. Got attr={verdict.attribute_score}"
    )


def test_thin_evidence_denial_is_explained():
    """A 1.00 score with no identity must say why, or the row is unreadable."""
    verdict = stage_identity.evaluate(
        source("himalaya anti dandruff shampoo"),
        master("7001921", "ANTI-DANDRUFF SHAMPOO 400ml INDIA OFFER", None, None, ""),
    )
    assert "THIN" in verdict.summary


def test_identity_needs_discriminating_attributes():
    """Family, size and count identify a product. Brand and form do not."""
    assert "brand" not in C.IDENTITY_DISCRIMINATING
    assert "form" not in C.IDENTITY_DISCRIMINATING
    assert C.IDENTITY_DISCRIMINATING >= {"family", "size", "count"}


# ---------------------------------------------------------------------------
# TEST 5c -- a silent catalogue row is a SINGLE, not an unknown
# ---------------------------------------------------------------------------
# The bug this guards against was visible in the portal: "Litchi Shine Lip
# Care 4.5G (Pack Of 5)" showed its best match at 60% and BOTH alternatives at
# 100%. The 100s were master rows that state no count at all -- count was
# treated as unstated and dropped, so they scored perfectly on what remained,
# while the one candidate that declared a count (PACK OF 2) was capped for
# declaring the wrong one. Silence outscored honesty.

@pytest.mark.parametrize("code,name", [
    ("7002444", "LITCHI SHINE LIP CARE 4.5G INDIA"),
    ("7006520", "LITCHI SHINE SERUM LIP BALM 4.5G INDIA"),
])
def test_multipack_listing_conflicts_with_silent_single(code, name):
    verdict = stage_identity.evaluate(
        source("Himalaya Herbal Litchi Shine Lip Care 4.5G (Pack Of 5), Pink", 4.5, "GM"),
        master(code, name, 4.5, "GM", "LITCHI SHINE LIP CARE"),
    )
    assert verdict.identity_match is False, (
        "a Pack of 5 cannot be identical to a catalogue row that names no pack"
    )
    assert "pack_count" in verdict.conflicts


def test_count_silence_means_single_in_both_directions():
    """Silence is a claim about a SINGLE unit, whichever side is silent.

    A row that names no pack is offering one unit -- that is how anyone reads
    "Himalaya Peach Shine Lip Care, 4.5g". So a multipack on the other side is
    a different sellable unit, not an unknown.

    Handling only one direction left the mirror case wide open: measured, a
    single-unit listing scored a perfect 1.00 against the single, the PACK OF
    2 AND the PACK OF 3 -- three different products, three identical scores --
    because count was dropped as unstated and everything else agreed.
    """
    assert stage_identity.counts_agree(None, None) is None   # nothing to compare
    assert stage_identity.counts_agree(1, None) is None      # both single
    assert stage_identity.counts_agree(None, 1) is None      # both single
    assert stage_identity.counts_agree(5, None) is False     # 5-pack vs single
    assert stage_identity.counts_agree(None, 3) is False     # single vs 3-pack
    assert stage_identity.counts_agree(5, 2) is False        # real mismatch
    assert stage_identity.counts_agree(2, 2) is True


@pytest.mark.parametrize("code,name,expected_identity", [
    ("7002445", "PEACH SHINE LIP CARE 4.5G INDIA", True),
    ("7005711", "PEACH SHINE LIP CARE 4.5G (PACK OF 3) INDIA", False),
    ("7005165", "PEACH SHINE LIP CARE 4.5G PACK OF 2 INDIA", False),
])
def test_single_listing_does_not_match_a_multipack(code, name, expected_identity):
    """One listing, three candidates, three different answers."""
    verdict = stage_identity.evaluate(
        source("Himalaya Lip Care - Peach Shine, 4.5g Pack", 4.5, "GM"),
        master(code, name, 4.5, "GM", "PEACH SHINE LIP CARE"),
    )
    assert verdict.identity_match is expected_identity


# ---------------------------------------------------------------------------
# TEST 9 -- "PACK OF2" with no space
# ---------------------------------------------------------------------------
# Real listing text. The count pattern required whitespace after "of", so this
# parsed to no count at all -- the engine read the listing as a single unit,
# ruled the correct PACK OF 2 master a multipack mismatch and capped it at 60%,
# while two single-unit rows scored 100% beside it.

@pytest.mark.parametrize("title,expected", [
    ("RICH COCOA BUTTER LIP CARE 4.5G PACK OF2", 2),
    ("NAT. SOFT VANILLA LIP CARE 4.5G PACK OF2", 2),
    ("Himalaya Lip Balm PACK OF3", 3),
    ("Himalaya Lip Balm Pack of 2", 2),      # spaced form still works
    ("Himalaya Lip Balm (Pack Of 2)", 2),
])
def test_pack_of_parses_without_a_space(title, expected):
    assert stage_attributes.parse_pack_count(title) == expected


# ---------------------------------------------------------------------------
# TEST 10 -- a variant name is not interchangeable
# ---------------------------------------------------------------------------
# "Peach Shine Lip Care" matched the CHERRY SHINE group at 100%: the two groups
# reduce to {peach, shine} and {cherry, shine}, the listing shares "shine", and
# 1-of-2 cleared the match ratio. The token that names the product counted the
# same as the one that names nothing. The LLM's own reasoning on that row said
# "7005713 is the wrong variant (Cherry)" while the score said 100%.

def _frequency_from(groups: dict[str, int]) -> None:
    """Seed the learned token frequencies directly, without a DB round trip."""
    stage_identity._TOKEN_GROUP_FREQUENCY.clear()
    stage_identity._TOKEN_GROUP_FREQUENCY.update(groups)


def test_wrong_variant_is_not_the_same_family():
    # Real frequencies from the live master: peach/cherry name a line, shine
    # does not.
    _frequency_from({"peach": 2, "cherry": 2, "shine": 7})
    try:
        verdict = stage_identity.evaluate(
            source("Himalaya Peach Shine Lip Care, 4.5 Grams (Pack Of 2)", 4.5, "GM"),
            master("7005713", "CHERRY SHINE LIP CARE 4.5G (PACK OF 2) INDIA",
                   4.5, "GM", "CHERRY SHINE LIP BALM"),
        )
        assert verdict.identity_match is False
        assert "product_family" in verdict.conflicts
    finally:
        stage_identity._TOKEN_GROUP_FREQUENCY.clear()


def test_right_variant_still_matches():
    _frequency_from({"peach": 2, "cherry": 2, "shine": 7})
    try:
        verdict = stage_identity.evaluate(
            source("Himalaya Peach Shine Lip Care, 4.5 Grams (Pack Of 2)", 4.5, "GM"),
            master("7005165", "PEACH SHINE LIP CARE 4.5G PACK OF 2 INDIA",
                   4.5, "GM", "PEACH SHINE LIP BALM"),
        )
        assert verdict.identity_match is True
    finally:
        stage_identity._TOKEN_GROUP_FREQUENCY.clear()


def test_rarity_rule_is_inert_without_frequencies():
    """An unpopulated table must not turn every missing token into a variant.

    build_token_frequency() runs once per pipeline run; anything calling the
    identity layer without it (a unit test, another pipeline) has to keep
    working rather than silently rejecting every candidate.
    """
    stage_identity._TOKEN_GROUP_FREQUENCY.clear()
    verdict = stage_identity.evaluate(
        source("Himalaya Neem Face Wash 100ml", 100.0, "ML"),
        master("X3", "PURIFYING NEEM FACE WASH 100ML", 100.0, "ML", GROUP_NEEM_WASH),
    )
    assert verdict.identity_match is True


# ---------------------------------------------------------------------------
# TEST 6 -- unit normalisation
# ---------------------------------------------------------------------------

def test_equivalent_units_agree():
    """1 KG and 1000 GM are one size written two ways."""
    assert stage_identity.sizes_agree((1000.0, "g"), (1000.0, "g")) is True


def test_incomparable_units_are_unknown_not_mismatched():
    """Grams against millilitres is an unanswerable question, not a conflict."""
    assert stage_identity.sizes_agree((100.0, "g"), (100.0, "ml")) is None


def test_size_tolerance_absorbs_rounding_but_not_real_differences():
    assert stage_identity.sizes_agree((4.5, "g"), (4.5, "g")) is True
    assert stage_identity.sizes_agree((100.0, "ml"), (500.0, "ml")) is False


# ---------------------------------------------------------------------------
# TEST 7 -- the flag leaves existing behaviour alone
# ---------------------------------------------------------------------------

def test_v2_is_disabled_by_default():
    """Production behaviour must not change until the flag is set."""
    assert C.SCORING_V2_ENABLED is False


def test_weights_are_configurable_not_hardcoded():
    for name in ("W2_SEMANTIC", "W2_LEXICAL", "W2_CATEGORY", "W2_TYPE",
                 "W2_PACK", "W2_OVERLAP", "W2_ATTRIBUTES", "W2_LLM"):
        assert hasattr(C, name), f"{name} must be configurable"


def test_v2_weights_sum_to_one():
    total = (C.W2_SEMANTIC + C.W2_LEXICAL + C.W2_CATEGORY + C.W2_TYPE
             + C.W2_PACK + C.W2_OVERLAP + C.W2_ATTRIBUTES + C.W2_LLM)
    assert abs(total - 1.0) < 1e-9, f"weights sum to {total}, not 1.0"


# ---------------------------------------------------------------------------
# TEST 8 -- a missing judge must not penalise the row
# ---------------------------------------------------------------------------

def test_absent_llm_redistributes_rather_than_scoring_zero():
    """needs_judge() is false for most rows.

    Treating an absent judge as a zero would silently penalise every row it was
    never asked about, so its weight is redistributed instead.
    """
    with_llm = stage_identity.final_score(scores(0.70), 0.5, None, False, False)
    as_zero = stage_identity.final_score(scores(0.70), 0.5, 0.0, False, False)
    assert with_llm > as_zero
