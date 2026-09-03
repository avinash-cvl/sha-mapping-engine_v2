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


def parse_pack(text: str) -> tuple[float, str] | None:
    """Returns (normalized_value, normalized_unit) or None if no single-value
    pack notation was found. Values are normalized to grams or millilitres."""
    match = _PACK_RE.search(text.lower())
    if not match:
        return None
    raw_value, raw_unit = match.groups()
    unit = raw_unit.lower()
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
    re.compile(r"\bpack\s+of\s+(\d{1,4})\b", re.IGNORECASE),
    re.compile(r"\bset\s+of\s+(\d{1,4})\b", re.IGNORECASE),
    re.compile(r"\bcombo\s+of\s+(\d{1,4})\b", re.IGNORECASE),
    # "6N(5N+FREE 1N)" and "1X20N" both state the count with an N suffix.
    # The 1X20N form has to be read before the generic count-x-size pattern
    # below, which would otherwise take the leading "1" and discard it as
    # below the 2-unit floor -- losing a 20-pack entirely.
    re.compile(r"(?<![\w.])\d{1,4}\s*[x*]\s*(\d{1,4})\s*[nN]\b", re.IGNORECASE),
    re.compile(r"(?<![\w.])(\d{1,4})\s*[nN]\s*\(", re.IGNORECASE),
    re.compile(r"(?<![\w.])(\d{1,4})\s*[x*]\s*\d", re.IGNORECASE),
    re.compile(r"(?<![\w.])(\d{1,4})\s*(?:pcs?|nos?|units?|count)\b", re.IGNORECASE),
    re.compile(r"(?<![\w.])(\d{1,4})\s*['’ʼ]\s*s\b", re.IGNORECASE),
)

# Above this, a number is a weight/volume or a marketing figure, not a unit
# count -- "LIP BALM 5G JAR (1X20N)" is a 20-pack, "SAVE RS.8" is not an
# 8-pack. Matches the 2..24 band V2's pack_count() settled on, widened to 99
# because this master really does carry 24x and 26x cases.
_MAX_PACK_COUNT = 99


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
        parsed = normalize_pack(source.pack_value, source.pack_unit)
    if parsed is None:
        return source
    value, unit = parsed
    return replace(source, pack_value=value, pack_unit=unit)
