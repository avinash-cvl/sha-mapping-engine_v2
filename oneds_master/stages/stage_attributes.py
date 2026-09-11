"""Stage 3 -- product intelligence: attribute extraction.

Rule-based pack parsing (deterministic, cheap) runs on every row. Whether
to also call the LLM sub_brand/variant fallback (agents/attribute_fallback.py)
is decided in flow.py, not here -- this file stays a plain, LLM-free function
so it's trivial to unit-test.
"""
from __future__ import annotations

import re
from dataclasses import replace

from common.models import SourceProduct

# Single-value pack pattern: "50g", "100 ml", "1kg". Multipack notation
# ("2N X 400G", "700G+400G", "100*3ml") is deliberately NOT handled here --
# the prior art's equivalent regex silently mis-parsed "2N X 400G" (captured
# the digits after "X" instead of the leading count), and founding_doc.md
# section B.4 calls a correct multipack parser out as the highest-value fix.
# Build that as new, tested code -- do not extend this regex to "handle" it.
_PACK_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|gm|g|ml|l)\b", re.IGNORECASE)

# "ltr"/"gms" appear only in the structured master/source columns, never in
# the free-text titles _PACK_RE reads, so they are listed here for
# normalize_pack() rather than added to that regex. Unit counts ("NOS",
# "TAB", "CAP") are deliberately absent: a count is not a net measure, so it
# must fall through to a neutral score instead of being compared to grams.
_UNIT_NORMALIZE = {"gm": "g", "gms": "g", "g": "g", "kg": "g",
                   "ml": "ml", "l": "ml", "ltr": "ml"}
_UNIT_MULTIPLIER = {"gm": 1.0, "gms": 1.0, "g": 1.0, "kg": 1000.0,
                    "ml": 1.0, "l": 1000.0, "ltr": 1000.0}

# Plausibility bounds on a normalised size, in grams/millilitres.
#
# Sizes arrive from two places and both can lie. The staging pack_size/uom
# columns are populated upstream and carry real errors -- "Himalaya Since
# 1930 ... Lip Balm" is stored as 1930 L, a year read as a pack size, which
# made pack_score meaningless for that row and put a "Pack Of 1" listing on
# a 6-unit blister. 146 amazon rows carry a year-like size, and the columns
# reach 84,935,374 at the extreme. Free-text titles have the same failure
# mode (percentages, SPF numbers, years).
#
# Bounds are per RAW unit, not on the normalised gram value, because one
# global ceiling cannot do both jobs: it must clear 30 LTR (30000 ml, real
# bulk in this master) while rejecting "1930" read off "Since 1930". Those
# are the same normalised magnitude. Judging the number in the unit it was
# written in separates them -- 30 is a plausible LTR, 1930 is not.
#
# Ceilings come from the master, the only catalogue a row can map to: its
# largest entries are 1500 ML, 900 GM, 25 KG and 30 LTR. Each bound leaves
# headroom for a larger competitor pack without admitting a year.
_MAX_BY_UNIT = {"g": 5000.0, "gm": 5000.0, "gms": 5000.0,
                "ml": 5000.0, "kg": 100.0, "l": 100.0, "ltr": 100.0}
_MIN_PACK_VALUE = 0.1


def _plausible(raw_value: float, unit_key: str) -> bool:
    """Is this a believable pack size, judged in the unit as written?

    Out-of-range means "unknown", not "mismatch": pack_score() returns a
    neutral 0.5 for a missing size, which is the right treatment for a value
    we cannot trust. Never silently clamp -- a clamped value would read as a
    real measurement and score against real candidates.
    """
    return _MIN_PACK_VALUE <= raw_value <= _MAX_BY_UNIT.get(unit_key, 5000.0)


def parse_pack(text: str) -> tuple[float, str] | None:
    """Returns (normalized_value, normalized_unit) or None if no single-value
    pack notation was found. Values are normalized to grams or millilitres."""
    match = _PACK_RE.search(text.lower())
    if not match:
        return None
    raw_value, raw_unit = match.groups()
    unit = raw_unit.lower()
    if not _plausible(float(raw_value), unit):
        return None
    return float(raw_value) * _UNIT_MULTIPLIER[unit], _UNIT_NORMALIZE[unit]


# Unit-count notation. The master states counts only inside its title, in
# several shapes, and each of these is a real example from the data:
#   "(PACK OF 3)" / "PACK OF 2"        parenthesised or bare
#   "LIP BALM 24x10g"                  count x unit-size
#   "6N(5N+FREE 1N) X 10G"             N-suffixed, with a free-goods rider
#   "BABY DIAPERS MEDIUM 54'S"         apostrophe-S count
# Ordered most-specific first; _parse_count() takes the first that matches.
#
# The apostrophe-S form is deliberately last and tightly bounded: it is the
# loosest pattern, and without the leading (?<![\w.]) guard it would read the
# "5" out of "5S" inside a word. Upstream flagged this notation as unparsed
# entirely, which left diaper listings with no count at all.
_COUNT_PATTERNS = (
    # \s* not \s+ after "of": real listings run the number straight on, and
    # "RICH COCOA BUTTER LIP CARE 4.5G PACK OF2" parsed to no count at all.
    # The row then compared as a single unit, so the correct 2-pack master
    # scored no better than a single -- measured on amazon lip balms, where
    # the matching PACK OF 2 lost to two single-unit candidates.
    re.compile(r"\bpack\s*of\s*(\d{1,4})\b", re.IGNORECASE),
    re.compile(r"\bset\s*of\s*(\d{1,4})\b", re.IGNORECASE),
    re.compile(r"\bcombo\s*of\s*(\d{1,4})\b", re.IGNORECASE),
    # "6N(5N+FREE 1N)" and "1X20N" both state the count with an N suffix.
    # The 1X20N form has to be read before the generic count-x-size pattern
    # below, which would otherwise take the leading "1" and discard it as
    # below the 2-unit floor -- losing a 20-pack entirely.
    re.compile(r"(?<![\w.])\d{1,4}\s*[x*]\s*(\d{1,4})\s*[nN]\b", re.IGNORECASE),
    re.compile(r"(?<![\w.])(\d{1,4})\s*[nN]\s*\(", re.IGNORECASE),
    # SIZE x COUNT -- "150g x 2", "200ml *2", "8 g x 12".
    #
    # Must precede the COUNT x SIZE pattern below, which would otherwise read
    # the leading "150" of "150g x 2" as the count. The two notations share
    # the same NxM shape and mean opposite things; what separates them is
    # which side carries the unit. Here the unit is on the LEFT, so the left
    # number is the size and the right is the count. In "24x10g" the unit is
    # on the right, so the left number IS the count -- handled below, and it
    # stays correct because this pattern requires a unit first.
    #
    # Without it the same pack parsed differently depending only on how the
    # seller wrote it: "12 x 8g" gave 12 while "8g x 12" gave nothing.
    # Measured: 15 source listings and 13 master rows use the size-first form
    # -- "Complete Care 300g (150g x 2)", "Baby Cream 400ml (200ml *2 Pack)".
    re.compile(
        r"(?<![\w.])\d+(?:\.\d+)?\s*(?:kg|gms|gm|g|ml|ltr|l)\s*[x*]\s*(\d{1,4})\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?<![\w.])(\d{1,4})\s*[x*]\s*\d", re.IGNORECASE),
    # "60 Count", "60 Pieces", "30 Tablets", "10 Sachets". Tablet/capsule
    # counts matter here in a way they do not for toiletries: the master
    # states them as the ONLY size a supplement row carries ("NEEM TABLETS
    # 60'S"), so a listing's "60 Tablets" is the sole comparable attribute.
    # Without "pieces"/"tablets", "Skin Wellness Tablets - Neem, 60 Pieces
    # Box" parsed to no count at all and every candidate scored a flat 0.5.
    re.compile(
        r"(?<![\w.])(\d{1,4})\s*"
        r"(?:pcs?|pieces?|nos?|units?|count|tablets?|tabs?|capsules?|caps?|"
        r"sachets?|wipes?|sheets?)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?<![\w.])(\d{1,4})\s*['’ʼ]\s*s\b", re.IGNORECASE),
)

# Above this, a number is a weight/volume or a marketing figure, not a unit
# count -- "LIP BALM 5G JAR (1X20N)" is a 20-pack, "SAVE RS.8" is not an
# 8-pack.
#
# 999, not 99: supplements are counted in hundreds and the master really
# does carry "NEEM TABLETS 500'S". At the old 99 ceiling that row parsed to
# None, so a 60-count listing scored a NEUTRAL 0.5 against it -- "unknown"
# rather than "wrong" -- and the 500's outranked the correct 60's. A 500-vs-60
# disagreement is a real mismatch and has to score like one.
#
# The ceiling still earns its keep at 999: it rejects years ("Since 1930"),
# rupee figures and SPF/percentage numbers, which is what it was for. Sizes
# in grams/millilitres are not at risk either way -- they are read by
# parse_pack(), which requires an explicit unit.
_MAX_PACK_COUNT = 999

# The largest pack_count that may be used to divide a staging pack_size back
# down to a unit size (see extract_attributes). Deliberately far below
# _MAX_PACK_COUNT: a count that large alongside a pack_size it divides evenly
# is almost always a unit word misread as a quantity ("Pack of 200 Gram"), and
# dividing on it silently destroys a real measurement. Measured across 686
# himalaya multipack rows, every genuine retail multipack is <= 12.
_MAX_DIVISIBLE_COUNT = 12


def parse_pack_count(text: str | None) -> int | None:
    """Unit count stated in a title, or None when the title says nothing.

    None is NOT 1: "says nothing" and "says one" have to stay distinguishable
    so pack_count_score() can stay neutral on silence rather than asserting a
    single. Most listings and master rows genuinely say nothing.
    """
    if not text:
        return None
    for pattern in _COUNT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            count = int(match.group(1))
        except (TypeError, ValueError):
            continue
        if 2 <= count <= _MAX_PACK_COUNT:
            return count
    return None


def pack_count_score(
    source_count: int | None,
    master_count: int | None,
) -> float:
    """Agreement between two unit counts, 0-1.

    Neutral 0.5 whenever either side is silent -- and silence is the common
    case, so this must not read as a mismatch. A stated count on both sides
    that agrees scores 1.0; a disagreement falls away with the ratio, so a
    2-pack against a 3-pack still scores better than a 2-pack against a
    24-pack.
    """
    if source_count is None or master_count is None:
        return 0.5
    if source_count == master_count:
        return 1.0
    return min(source_count, master_count) / max(source_count, master_count)


def normalize_pack(
    value: float | str | None,
    unit: str | None,
) -> tuple[float, str] | None:
    """Normalises an already-structured (value, unit) pair to the same
    grams/millilitres convention parse_pack() produces, so both sides of
    pack_score() speak one vocabulary.

    parse_pack() only reads sizes out of free text. Rows that already carry
    a numeric size in a column (staging.himalaya_products.normalized_pack_size
    / normalized_uom) need the same unit folding applied, or a master 'GM'
    meets a source 'g' and pack_score() -- which treats a unit mismatch as a
    hard 0.0 -- scores every candidate zero.

    Returns None when either half is missing or the unit is not a
    weight/volume we can compare. 'NOS' (unit counts) and the literal string
    'NULL' both land here on purpose: a count is not a net measure, and
    scoring it against grams would be worse than the neutral 0.5 that a
    None yields.
    """
    if value is None or unit is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    key = str(unit).strip().lower()
    if key not in _UNIT_NORMALIZE:
        return None
    if not _plausible(numeric, key):
        return None
    return numeric * _UNIT_MULTIPLIER[key], _UNIT_NORMALIZE[key]


def pack_score(
    source_pack: tuple[float, str] | None,
    master_pack: tuple[float, str] | None,
) -> float:
    """Ratio-based pack agreement, symmetric, 1.0 = exact match. Missing
    pack info on either side scores a neutral 0.5 -- a parse failure
    shouldn't read as a hard mismatch."""
    if source_pack is None or master_pack is None:
        return 0.5
    (source_value, source_unit), (master_value, master_unit) = source_pack, master_pack
    if source_unit != master_unit or master_value == 0:
        return 0.0
    return min(source_value, master_value) / max(source_value, master_value)


def extract_attributes(source: SourceProduct) -> SourceProduct:
    """Populate pack_value/pack_unit via rules. sub_brand/variant are left
    None here -- flow.py calls agents.attribute_fallback.extract() for rows
    where they're still missing after this."""
    parsed = parse_pack(source.title)
    if parsed is None:
        # No size in the title, but the row may already carry one from its
        # staging columns (build_source_product copies pack_size/uom through
        # verbatim). Fold that to the same grams/millilitres convention the
        # title path produces -- otherwise a raw 'GM' meets the master's
        # normalised 'g' and pack_score() scores an exact 10g/10g match 0.0,
        # because it treats a unit mismatch as a hard zero.
        #
        # The staging pack_size is the COMBINED weight on a multipack listing,
        # while the master states the size of ONE unit -- so a "Pack of 3" of a
        # 30g sheet mask arrives here as 90g and pack_score() reads 30/90=0.33
        # against the very row it should match. Divide it back down to the unit
        # size when the row states a count.
        #
        # Only on this branch, and deliberately so: when the TITLE carries a
        # size, parse_pack() already returned the unit size (measured on 645
        # himalaya multipack rows, 428 state the unit size in the title and are
        # handled above) and dividing again would be wrong. This path is only
        # reached when the title is silent, which is exactly when the staging
        # column is the sole source and its combined reading goes unchallenged.
        #
        # Guarded by divisibility: a clean quotient means the value really was
        # count x unit. A tablet count or a mg strength that merely happens to
        # sit alongside a pack_no would not divide evenly, and is left alone.
        # _MAX_DIVISIBLE_COUNT guards against a count that is really a weight:
        # "Himalaya Baby Powder (Pack of 200 Gram)" parses to count=200, and
        # dividing its 200g by that yields a 1g baby powder. A genuine retail
        # multipack is small -- measured across 686 himalaya multipack rows,
        # every true one is <= 12 -- so a larger "count" against a pack_size it
        # happens to divide is the unit word being read as a quantity.
        staging_value = source.pack_value
        if (
            staging_value is not None
            and source.pack_count is not None
            and 1 < source.pack_count <= _MAX_DIVISIBLE_COUNT
            and float(staging_value) % source.pack_count == 0
            and float(staging_value) / source.pack_count > 0
        ):
            staging_value = float(staging_value) / source.pack_count
        parsed = normalize_pack(staging_value, source.pack_unit)
    if parsed is None and source.pack_unit is None:
        # A size with no unit at all. The staging uom column is NULL on a
        # large share of rows, and dropping those would throw away a real
        # measurement -- "Himalaya Shine Lip Care" carries 4.5 with no unit,
        # and 4.5 is exactly the chapstick size. Assume grams, the unit
        # _UNIT_NORMALIZE folds weights to, and let the plausibility bound
        # still apply.
        #
        # This is strictly better than what came before: an unnormalised
        # (4.5, None) reached pack_score() and scored a hard 0.0 against a
        # (4.5, 'g') master row, because a unit mismatch is treated as a
        # mismatch outright. Inferring the unit turns that into the 1.0 it
        # should always have been.
        parsed = normalize_pack(source.pack_value, "g")
    if parsed is None:
        # Nothing usable. Clear the field rather than returning the row
        # untouched: pack_value may still hold the raw, rejected staging
        # value (an implausible 1930 L, or a unit like NOS that is a count
        # not a measure), and leaving it there would send it straight into
        # pack_score(). A None scores a neutral 0.5, which is the honest
        # answer for a size we could not trust.
        if source.pack_value is None and source.pack_unit is None:
            return source
        return replace(source, pack_value=None, pack_unit=None)
    value, unit = parsed
    return replace(source, pack_value=value, pack_unit=unit)
