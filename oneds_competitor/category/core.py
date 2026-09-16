"""
core.py -- shared engine for 1DS -> master category mapping.

Vendored from V2's `mapping_code/mapping_core.py`, renamed throughout to V1's
vocabulary. What V2 called "HGML category / sub-category" is exactly
master_category / master_subcategory: the columns of
config.oneds_master_category_mapping, the values in
staging.himalaya_products.normalized_category / _subcategory, and the
(category, subcategory) key of the master_lookup dict built in
step_3_build_master_lookup. Same nodes in the same Material Master -- the
names differed only because the codebases were written separately, so the
HGML spelling is gone from this port. The one practical difference is casing:
this module emits the master's own casing ("FACE WASH"), while V1 lowercases
at every lookup -- so callers normalise with .strip().lower().

Changes made when vendoring (see v2_integration_prompt.md Part 1):
  - Dropped `import pandas as pd` and the pandas-only helpers run_category(),
    summarise(), audit_sample() and validate(). V1 needs none of them, and
    the pandas import is what made the original module unimportable here.
    validate()'s job -- "every mapped target must exist in the Material
    Master" -- is done instead by pack_targets() in resolve.py, which works
    off the packs rather than off a scored DataFrame.
  - Dropped V2's module-level THRESHOLD = 0.80 in favour of a single V1
    constant, common.config.CATEGORY_GATE_MIN_CONF.

Everything below that point is V2's logic verbatim -- do not "clean up" the
regexes or reorder classify()'s branches without re-running the audits that
produced them.

Design rules, each earned from a specific audit failure
-------------------------------------------------------
1. TWO-LEVEL SCORING. Category and sub-category are decided and scored
   independently. Deciding "this is lip care" is easy; deciding "chapstick or
   tube" is a packaging fact most titles omit. Collapsing them forced thousands
   of resolvable rows into review. (lip makeup)

2. STRUCTURED FIELDS BEAT TITLE KEYWORDS. Where 1DS carries a real attribute
   (subcategory, pet_food_type), route on it. Titles are seller copy, not
   product attributes. (pet care)

3. POSITIONING IS A PROPERTY OF THE BRAND LINE, NOT THE LISTING. Dabur Red
   landed in three different nodes across three rows because sellers wrote
   different copy. Brand lines are decided once and applied to every SKU.
   (oral healthcare)

4. HIMALAYA'S OWN SKUs ARE LOOKED UP, NEVER INFERRED. "Complete Care ... with
   Neem" went to BOTANIQUE on the word "neem"; the master files it under plain
   TOOTHPASTE. (oral healthcare)

5. FOUR TERMINAL STATES, not two. Conflating them hid the most commercially
   interesting output. (vitamins & supplements)
       <node>              mapped to a real master node
       NO MASTER EQUIVALENT  confident finding: Himalaya sells nothing here
       UNRESOLVED          genuine uncertainty -> review queue
       UNCLASSIFIED        not a product of this kind at all (devices, plants)

6. A CATEGORY WITH NO REVIEW QUEUE IS UNDER-CALIBRATED, NOT PERFECT. If every
   row auto-commits but measured accuracy is 91%, confidence is optimistic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

NO_EQ = "NO MASTER EQUIVALENT"
UNRES = "UNRESOLVED"
UNCLS = "UNCLASSIFIED"
TERMINALS = {NO_EQ, UNRES, UNCLS}

# --------------------------------------------------------------------------
# global pre-passes — apply to every category
# --------------------------------------------------------------------------

# Not a consumer formulation at all. 1DS carries saplings, crop chemicals,
# appliances and accessories inside product categories.
NOT_PRODUCT = re.compile(
    r"\bfor plants?\b|\bazadirachtin\b|\bpesticide\b|\binsecticide\b|\bsapling\b|"
    r"\bseeds? for (sowing|planting)\b|\bfertili[sz]er\b|\bsoil\b|"
    r"\bdispenser\b|\bwater flosser\b|\birrigator\b|\bmachine\b|\bdevice\b|"
    r"\bapplicator\b|\bhair dryer\b|\bstraightener\b|\btrimmer\b|\bclipper\b|"
    r"\bmirror\b|\bstorage box\b|\bpouch bag\b|\bgift wrap\b|"
    # PPE respirators are not skincare face masks
    r"\bn95\b|\bkn95\b|\bsurgical mask\b|\bface shield\b|\brespirator\b|"
    r"\bnonwoven fabric\b|\b3 ?ply mask\b")


def normalise(title: str) -> str:
    """Lowercase and strip punctuation. Underscores hide word boundaries:
    'Balm_Grapefruit' silently failed \\bbalm\\b before this was added."""
    return re.sub(r"[^a-z0-9.%&+]+", " ", str(title).lower())


def pack_count(t: str) -> int:
    """Multi-packs quote COMBINED weight. 'Pack of 2 ... 9g' is 2 x 4.5g
    sticks, not a 9g tube. (lip makeup)"""
    m = re.search(r"pack of (\d{1,2})|(\d{1,2})\s*(?:x|\*)\s*\d|set of (\d{1,2})|"
                  r"combo of (\d{1,2})|(\d{1,2})\s*pcs?\b", t)
    if not m:
        return 1
    n = next((int(g) for g in m.groups() if g), 1)
    return n if 2 <= n <= 24 else 1


SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(gm?s?\b|grams?\b|ml\b|l\b|kg\b)", re.I)


def parse_size(title: str, packsize, lo=0.5, hi=5000):
    for m in SIZE_RE.finditer(str(title)):
        v = float(m.group(1))
        if lo <= v <= hi:
            return v
    if isinstance(packsize, str):
        m = SIZE_RE.search(packsize)
        if m and lo <= float(m.group(1)) <= hi:
            return float(m.group(1))
    return None


# --------------------------------------------------------------------------
# rule model
# --------------------------------------------------------------------------

@dataclass
class Rule:
    """One ordered routing decision. First match wins."""
    label: str
    cat: str
    sub: str
    conf: float
    title: str | None = None      # regex over the normalised title
    subcats: tuple | None = None  # restrict to these 1DS sub-categories
    not_title: str | None = None  # veto if this matches

    _rx: re.Pattern | None = field(default=None, init=False, repr=False)
    _veto: re.Pattern | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self._rx = re.compile(self.title) if self.title else None
        self._veto = re.compile(self.not_title) if self.not_title else None

    def match(self, t: str, subcat: str):
        if self.subcats is not None and subcat not in self.subcats:
            return None
        if self._veto and self._veto.search(t):
            return None
        if self._rx is None:
            return self.label
        m = self._rx.search(t)
        return f"{self.label}: {m.group(0)}" if m else None


@dataclass
class CategoryPack:
    """Everything needed to map one 1DS category.

    Note the deliberately differing tuple arities -- him_lookup values are
    2-tuples, subcat_map values are 3-tuples, and default is a 4-tuple.
    Anything walking these (see resolve.pack_targets) must index, never
    unpack.
    """
    name: str
    rules: list                       # ordered Rule list
    default: tuple                    # (cat, sub, conf, label) when nothing matches
    him_lookup: dict = field(default_factory=dict)   # substring -> (cat, sub)
    subcat_map: dict = field(default_factory=dict)   # 1DS subcat -> (cat, sub, conf)


def classify(row, pack: CategoryPack):
    """Resolve one row against one pack.

    `row` needs .title, .subcategory and (optionally) .brand -- V1's
    SourceProduct carries all three, so it is passed in directly with no
    shim object.

    Returns a dict with six keys: cat, cat_conf, cat_evidence, sub, sub_conf,
    sub_evidence. Branch order is load-bearing: NOT_PRODUCT first, then
    Himalaya's own SKUs by lookup, then the ordered rules, then the 1DS
    sub-category, then the pack default.
    """
    t = normalise(row.title)
    subcat = str(row.subcategory)
    brand = str(getattr(row, "brand", "")).strip().lower()
    # brand column, not the title: "Himalayan salt" in a competitor listing
    # was making rival SKUs look like Himalaya's own. (oral healthcare)
    is_him = brand == "himalaya" or t.startswith("himalaya ")

    if NOT_PRODUCT.search(t):
        lbl = f"not a formulation: {NOT_PRODUCT.search(t).group(0)}"
        return dict(cat=UNCLS, cat_conf=0.88, cat_evidence=lbl,
                    sub=UNCLS, sub_conf=0.88, sub_evidence=lbl)

    if is_him:
        for key, (c, s) in pack.him_lookup.items():
            if key in t:
                lbl = f"Himalaya master match: {key}"
                return dict(cat=c, cat_conf=0.97, cat_evidence=lbl,
                            sub=s, sub_conf=0.97, sub_evidence=lbl)

    for r in pack.rules:
        hit = r.match(t, subcat)
        if hit:
            return dict(cat=r.cat, cat_conf=min(0.97, r.conf + (0.02 if is_him else 0)),
                        cat_evidence=hit, sub=r.sub,
                        sub_conf=min(0.97, r.conf + (0.02 if is_him else 0)),
                        sub_evidence=hit)

    if subcat in pack.subcat_map:
        c, s, cf = pack.subcat_map[subcat]
        lbl = f"1DS sub-category = {subcat}"
        return dict(cat=c, cat_conf=cf, cat_evidence=lbl,
                    sub=s, sub_conf=cf, sub_evidence=lbl)

    c, s, cf, lbl = pack.default
    return dict(cat=c, cat_conf=cf, cat_evidence=lbl, sub=s, sub_conf=cf, sub_evidence=lbl)
