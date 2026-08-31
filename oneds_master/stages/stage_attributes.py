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

_UNIT_NORMALIZE = {"gm": "g", "g": "g", "kg": "g", "ml": "ml", "l": "ml"}
_UNIT_MULTIPLIER = {"gm": 1.0, "g": 1.0, "kg": 1000.0, "ml": 1.0, "l": 1000.0}


def parse_pack(text: str) -> tuple[float, str] | None:
    """Returns (normalized_value, normalized_unit) or None if no single-value
    pack notation was found. Values are normalized to grams or millilitres."""
    match = _PACK_RE.search(text.lower())
    if not match:
        return None
    raw_value, raw_unit = match.groups()
    unit = raw_unit.lower()
    return float(raw_value) * _UNIT_MULTIPLIER[unit], _UNIT_NORMALIZE[unit]


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
        return source
    value, unit = parsed
    return replace(source, pack_value=value, pack_unit=unit)
