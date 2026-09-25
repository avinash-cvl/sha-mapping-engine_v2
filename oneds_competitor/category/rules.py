"""
rules.py — one CategoryPack per remaining 1DS category.

Vendored verbatim from V2's `mapping_code/category_rules.py`; the only edit
is the import below. PACKS is the importable form of V2's rules -- the
`categories/*.py` scripts are NOT vendored, because they do
`sys.path.insert(0, "..")` and (for the four pilots) call load_master() at
module import time, so they cannot be used as a library.

PACKS now covers all 25 of V2's categories. The four hand-audited ones --
lip_makeup, pet_care, oral_healthcare and vitamins_supplements -- were the
late port at the end of this file; see the block comment there for what was
and was not reproduced from V2's scripts.

Rules are ordered; first match wins. Targets are taken verbatim from the
Material Master, and batch_flow.main() asserts at startup that every one
exists (see resolve.pack_targets).

Where a 1DS sub-category has no Himalaya counterpart the pack says so
explicitly with NO MASTER EQUIVALENT rather than forcing a near-miss. Those rows
are the portfolio white space and are the most commercially useful output here
— body wash gels, electrolytes and coconut water are all sizeable segments
Himalaya does not currently play in.
"""
from oneds_competitor.category.core import NO_EQ, UNRES, CategoryPack, Rule

# Himalaya product lookups are GENERATED from the Material Master, never typed.
# The earlier V2 bundle hand-wrote these dicts, and a bare "purifying neem" key
# mapped every Purifying Neem FACE WASH to FACE MASKS -- 113 misclassified
# listings upstream, and 50 of 62 rank-1 changes when measured here on one
# amazon face-wash group. Generated keys carry the form ("purifying neem face
# wash" vs "purifying neem mask rinse off"), so they cannot contradict the
# master.
#
# Each pack takes a SCOPED slice: only the phrases whose master_category is one
# this 1DS category can legitimately reach, so a pure-herb key cannot claim an
# oral-care listing.
from oneds_competitor.category.him_lookup import HIM_LOOKUP as _GEN


def _scoped(*master_categories):
    cats = set(master_categories)
    return {k: v for k, v in _GEN.items() if v[0] in cats}


# master_category constants (config.oneds_master_category_mapping vocabulary)
BABY_OTH = "BABY CARE - OTHERS"
BABY_DIA = "BABY DIAPERS"
BABY_TOI = "BABY TOILETRIES"
BABY_WIP = "BABY WIPES"
BODY_MOI = "BODY MOISTURISERS"
FACE_OTH = "FACE CARE - OTHERS"
FACE_CLN = "FACE CLEANSERS EXCL. FACE WASH"
FACE_MOI = "FACE MOISTURISERS"
FACE_SER = "FACE SERUMS"
FACE_WSH = "FACE WASH"
HAIR = "HAIR CARE"
MENS = "MENS CARE"
PERS = "PERSONAL HYGIENE"
SUN = "SUN CARE"
OTX_F = "OTX - FORMULATIONS"
OTX_FF = "OTX - FUNCTIONAL FOODS"
OTX_O = "OTX - OTHERS"
OTX_PS = "OTX - PARTYSMART"
OTX_PO = "OTX - PURE HERBS - OTHERS"
OTX_PORG = "OTX - PURE HERBS - ORGANIC"
PH_F = "PHARMA - FORMULATIONS"
PH_O = "PHARMA - OTHERS"
PH_PH = "PHARMA - PURE HERBS - OTHERS"
# Added with the four late ports at the end of this file. Spelled exactly as
# the live master spells them -- resolve.pack_targets() asserts at startup that
# every target exists, so a typo here fails the run rather than silently
# gating every row in the category down to nothing.
LIP = "LIP CARE"
ORAL = "ORAL CARE"
PET_FOOD = "COMPANION CARE - FOODS"
PET_GROOM = "COMPANION CARE - GROOMING"
PET_SUPP = "COMPANION CARE - SUPPLEMENTS"

ORGANIC = r"\borganic\b|\bcertified organic\b|\busda\b"
# Bare "men" (no apostrophe, no "for") is the majority spelling on these
# listings -- "Himalaya Men Active Sport Face Wash", "Himalaya MEN Power
# Glow Licorice Face Wash" -- and its absence here meant those listings
# never reached the MENS-CARE master node at all: the source's own category
# gate resolved to the generic ('face wash', 'face wash') node instead of
# ('MENS CARE', 'FACE WASH'), so the correct candidate was never even a
# retrieval hit, no matter how the scorer weighed it. Measured on blinkit
# 371833 (title has no apostrophe) and 67 other himalaya rows across
# zepto/blinkit/amazon with the same bare spelling.
MENS_CUE = r"\bfor men\b|\bmen'?s\b|\bmens\b|\bhomme\b|\bbeard\b|\bmen\b"
# Vetoes MENS_CUE above whenever the title also says "women" -- "For Both
# Men And Women", "For Women & Men" are unisex lines, not the Mens-Care
# range, and bare \bmen\b alone cannot tell the two apart.
NOT_UNISEX = r"\bwomen\b"
KIDS_CUE = r"\bkids?\b|\bchildren\b|\bbaby\b|\btoddler\b|\bjunior\b"

PACKS = {}

# ---------------------------------------------------------------- face care
PACKS["face care"] = CategoryPack(
    name="face care",
    # BODY_MOI is scoped in for the NOURISHING SKIN CREAM line: 1DS files it
    # as face care/face creams, the master as BODY MOISTURISERS / GP CREAMS.
    him_lookup=_scoped(FACE_WSH, FACE_CLN, FACE_MOI, FACE_SER, FACE_OTH, MENS, PH_F,
                       BODY_MOI),
    rules=[
        # Clarina and Bleminor ship as creams and washes only, so the
        # therapeutic route is restricted to those forms. Applying it to every
        # form sent a Fuller's-earth face pack to CLARINA.
        Rule("acne / pimple treatment line", PH_F, "CLARINA", 0.84,
             subcats=("face wash", "face creams"),
             title=r"\banti[- ]?acne\b|\bacne\b|\bpimple\b|\bblemish control\b"),
        Rule("pigmentation treatment line", PH_F, "BLEMINOR", 0.82,
             subcats=("face creams", "night creams"),
             title=r"\bpigmentation\b|\bdark spot\b|\bmelasma\b|\bde[- ]?tan\b"),
        Rule("men's face wash", MENS, "FACE WASH", 0.88,
             subcats=("face wash",), title=MENS_CUE, not_title=NOT_UNISEX),
        Rule("men's face cream", MENS, "FACE CREAMS", 0.86,
             subcats=("face creams", "night creams"), title=MENS_CUE, not_title=NOT_UNISEX),
        Rule("micellar water", FACE_OTH, "MICELLAR WATER", 0.92,
             title=r"\bmicellar\b"),
        Rule("cleansing milk / toner", FACE_CLN, "TONER / MILK", 0.90,
             subcats=("toners",)),
        Rule("sheet mask / face pack", FACE_CLN, "FACE MASKS", 0.92,
             subcats=("face masks", "face pack", "facial peels")),
        Rule("scrub", FACE_CLN, "FACE SCRUBS", 0.93,
             subcats=("facial scrubs & polishes",)),
        Rule("facial wipes", FACE_CLN, "FACIAL WIPES", 0.93,
             subcats=("facial wipes",)),
        # Routed to FACE MOISTURISERS, not FACE_SER. The master files 22 of
        # its 23 face serums under ('FACE MOISTURISERS', 'FACE SERUMS'); the
        # ('FACE SERUMS', 'FACE SERUMS') node holds exactly one row, the
        # DARK SPOT CLEARING TURMERIC *SERUM FLUID*. Targeting FACE_SER gated
        # every serum listing down to that single decoy, which then won by
        # default -- 5 QC misses, and 6 more listings pinned to it besides.
        # him_lookup agrees: every NAMED serum phrase there resolves to
        # FACE MOISTURISERS, and only the generic 'face serums' key points
        # here (a 2-vs-2 tie the generator broke the wrong way, see
        # him_lookup.AMBIGUOUS).
        Rule("serum", FACE_MOI, "FACE SERUMS", 0.91, subcats=("face serums",)),
        Rule("face wash", FACE_WSH, "FACE WASH", 0.94, subcats=("face wash",)),
        Rule("eye cream -> treatment creams", FACE_MOI, "TREATMENT CREAMS", 0.84,
             subcats=("eye creams",)),
        Rule("night cream -> premium creams", FACE_MOI, "PREMIUM CREAMS", 0.82,
             subcats=("night creams",)),
        Rule("face gel", FACE_MOI, "FACE GELS", 0.86,
             subcats=("face creams",), title=r"\bgel\b", not_title=r"\bcream\b"),
        # Two sibling nodes the bare "face cream" rule used to swallow.
        # Serum-creams and gel-creams are filed as PREMIUM CREAMS (24 rows),
        # and the NOURISHING SKIN CREAM line is a body/GP product filed
        # under BODY MOISTURISERS / GP CREAMS -- not FACE CREAMS. Both are
        # ordered before "face cream", which matches on sub-category alone
        # and would otherwise win.
        Rule("serum cream / gel cream -> premium creams", FACE_MOI, "PREMIUM CREAMS", 0.88,
             subcats=("face creams",), title=r"\bserum cream\b|\bgel cream\b"),
        Rule("nourishing skin cream -> GP creams", BODY_MOI, "GP CREAMS", 0.88,
             subcats=("face creams",), title=r"\bnourishing skin cream\b"),
        Rule("face cream", FACE_MOI, "FACE CREAMS", 0.90, subcats=("face creams",)),
        Rule("multi-step kit", FACE_OTH, "FACE CARE - OTHERS", 0.70,
             subcats=("facial kit",)),
    ],
    default=(UNRES, UNRES, 0.40, "face care, form not identified"),
)

# ---------------------------------------------------------------- skin care
PACKS["skin care"] = CategoryPack(
    name="skin care",
    # FACE_MOI is scoped in because 1DS files face gels/creams under
    # skin care/moisturizers while the master keeps them in FACE
    # MOISTURISERS -- without it those phrases are filtered out of
    # him_lookup and the row falls through to a BODY node.
    him_lookup=_scoped(PERS, BODY_MOI, SUN, OTX_O, FACE_CLN, FACE_MOI),
    rules=[
        # Himalaya sells bar soap and hand wash, but no shower gel / body wash.
        Rule("body wash / shower gel", NO_EQ, NO_EQ, 0.90,
             subcats=("body wash gels",)),
        Rule("hand sanitiser", PERS, "HAND SANITIZER", 0.93,
             title=r"\bsanitis?er\b|\bsanitizer\b"),
        Rule("hand wash", PERS, "HAND WASH", 0.93, subcats=("hand wash",)),
        Rule("bar soap", PERS, "BAR SOAPS", 0.93, subcats=("soaps",)),
        # The legacy "PROTECTIVE SUNSCREEN LOTION" line is filed under
        # BODY MOISTURISERS / BODY LOTIONS (183 rows), NOT under SUN CARE --
        # only the newer Sun Protect+ line sits in SUN CARE, and
        # SUN CARE / SUNSCREEN LOTION holds exactly one row. Without this
        # rule every legacy listing gated onto that single row and lost.
        # Ordered before the generic sunscreen rule: both match on
        # subcat='sunscreen' + "lotion", and first match wins.
        Rule("protective sunscreen lotion (legacy line)", BODY_MOI, "BODY LOTIONS", 0.90,
             subcats=("sunscreen",), title=r"\bprotective sunscreen\b"),
        Rule("sunscreen", SUN, "SUNSCREEN LOTION", 0.88,
             subcats=("sunscreen",), title=r"\blotion\b|\bfluid\b|\bmilk\b"),
        Rule("sunscreen cream", SUN, "SUNSCREEN CREAM", 0.88, subcats=("sunscreen",)),
        Rule("foot care", OTX_O, "FOOT CARE", 0.92, subcats=("foot cream",)),
        Rule("massage oil", OTX_O, "MASSAGE OIL", 0.90, subcats=("massage oils",)),
        Rule("cleansing milk", FACE_CLN, "TONER / MILK", 0.86,
             subcats=("cleansing creams & milks",)),
        Rule("body lotion", BODY_MOI, "BODY LOTIONS", 0.92, subcats=("body lotions",)),
        # A listing that says "FACE gel" is a face product even when 1DS
        # files it under skin care/moisturizers. The master keeps those in
        # FACE MOISTURISERS / FACE GELS, which this pack does not otherwise
        # reach, so they used to gate to BODY GELS and miss. Ordered before
        # "body gel", which would otherwise claim them on the bare "gel".
        Rule("face gel (face-named, inside skin care)", FACE_MOI, "FACE GELS", 0.88,
             subcats=("moisturizers",), title=r"\bface gel\b"),
        Rule("body gel", BODY_MOI, "BODY GELS", 0.84,
             subcats=("moisturizers",), title=r"\bgel\b", not_title=r"\bcream\b|\blotion\b"),
        Rule("body cream", BODY_MOI, "GP CREAMS", 0.88,
             subcats=("body creams", "body butters", "moisturizers")),
    ],
    default=(UNRES, UNRES, 0.40, "skin care, form not identified"),
)

# ---------------------------------------------------------------- hair care
PACKS["hair care"] = CategoryPack(
    name="hair care",
    him_lookup=_scoped(HAIR, MENS, PH_F),
    rules=[
        Rule("regrowth serum line", PH_F, "HAIR ZONE", 0.88,
             subcats=("hair regrowth treatments", "hair lotions")),
        Rule("men's hair gel", MENS, "HAIR GELS", 0.90, subcats=("hair gels",)),
        Rule("men's hair cream", MENS, "HAIR CREAMS", 0.86,
             subcats=("hair creams",), title=MENS_CUE, not_title=NOT_UNISEX),
        Rule("henna", HAIR, "HENNA", 0.93, subcats=("hennas",)),
        Rule("conditioner", HAIR, "CONDITIONER", 0.93, subcats=("conditioners",)),
        Rule("hair oil", HAIR, "HAIR OILS", 0.94, subcats=("hair oil",)),
        Rule("hair cream", HAIR, "HAIR CREAMS", 0.90, subcats=("hair creams",)),
        Rule("shampoo", HAIR, "SHAMPOOS", 0.94, subcats=("shampoos",)),
        # A shampoo+conditioner set is booked to its lead component.
        Rule("shampoo & conditioner set", HAIR, "SHAMPOOS", 0.80,
             subcats=("shampoo & conditioner sets",)),
    ],
    default=(UNRES, UNRES, 0.40, "hair care, form not identified"),
)

# ---------------------------------------------------------------- baby care
PACKS["baby care"] = CategoryPack(
    name="baby care",
    him_lookup=_scoped(BABY_TOI, BABY_WIP, BABY_OTH),
    rules=[
        Rule("botanique baby line", BABY_TOI, "BOTANIQUE BABY SOAPS", 0.86,
             subcats=("baby soaps",), title=r"\bbotanique\b|" + ORGANIC),
        Rule("botanique baby bath", BABY_TOI, "BOTANIQUE BABY BATH", 0.86,
             subcats=("baby body washes",), title=r"\bbotanique\b|" + ORGANIC),
        Rule("baby wipes", BABY_WIP, "BABY WIPES", 0.95, subcats=("baby wet wipes",)),
        Rule("baby laundry", BABY_OTH, "LAUNDRY", 0.94,
             subcats=("baby laundry detergents",)),
        Rule("kids toothpaste", BABY_TOI, "KIDS TOOTHPASTE", 0.94,
             subcats=("baby toothpaste",)),
        Rule("gift set", BABY_TOI, "GIFT SET", 0.90,
             subcats=("baby gift packs", "grooming kits")),
        Rule("diaper rash cream", BABY_TOI, "BABY CREAMS", 0.90,
             subcats=("diaper rash creams",)),
        Rule("baby bath", BABY_TOI, "BABY BATH", 0.93, subcats=("baby body washes",)),
        Rule("baby soap", BABY_TOI, "BABY SOAPS", 0.93, subcats=("baby soaps",)),
        Rule("baby shampoo", BABY_TOI, "BABY SHAMPOOS", 0.93, subcats=("baby shampoos",)),
        Rule("baby powder", BABY_TOI, "BABY POWDER", 0.93, subcats=("baby powders",)),
        Rule("baby lotion", BABY_TOI, "BABY LOTION", 0.93, subcats=("baby lotions",)),
        Rule("baby cream", BABY_TOI, "BABY CREAMS", 0.92, subcats=("baby creams",)),
        Rule("baby oil", BABY_TOI, "BABY OILS", 0.93,
             subcats=("baby oils", "baby hair oil")),
    ],
    default=(UNRES, UNRES, 0.40, "baby care, form not identified"),
)

# ------------------------------------------------------- herbal supplements
_HERB = {
    "ashwagandha": (OTX_PO, "ASHWAGANDHA"), "shilajit": (OTX_PO, "SHILAJIT"),
    "tulsi": (OTX_PO, "HOLY BASIL - TULASI"), "giloy": (OTX_PO, "GUDUCHI"),
    "triphala": (PH_PH, "TRIPHALA"), "shatavari": (PH_PH, "SHATAVARI - ASPARAGUS"),
    "amla": (PH_PH, "AMALAKI"), "arjuna": (PH_PH, "ARJUNA"),
    "karela": (PH_PH, "KARELA - BITTER MELON"),
}
PACKS["herbal supplements"] = CategoryPack(
    name="herbal supplements",
    him_lookup=_scoped(OTX_PO, OTX_PORG, PH_PH, OTX_F, PH_F),
    rules=[
        Rule("organic ashwagandha", OTX_PORG, "ASHWAGANDHA", 0.88,
             subcats=("ashwagandha",), title=ORGANIC),
        Rule("digestive formulation", OTX_F, "GASEX", 0.76,
             subcats=("digestion & nausea",)),
    ],
    subcat_map={k: (v[0], v[1], 0.94) for k, v in _HERB.items()},
    default=(UNRES, UNRES, 0.40, "herb not identified"),
)

# ------------------------------------------------------------------ diapers
PACKS["diapers"] = CategoryPack(
    name="diapers", rules=[],
    subcat_map={"diaper pants": (BABY_DIA, "DIAPERS / PANTS", 0.95),
                "taped diapers": (BABY_DIA, "DIAPERS / PANTS", 0.95)},
    default=(UNRES, UNRES, 0.40, "diaper form not identified"),
)

PACKS["adult diapers"] = CategoryPack(
    name="adult diapers", rules=[],
    subcat_map={"adult diapers & incontinence": (OTX_O, "ADULT DIAPERS", 0.95)},
    default=(UNRES, UNRES, 0.40, "not identified"),
)

# ------------------------------------------------------- drinks & beverages
PACKS["drinks & beverages"] = CategoryPack(
    name="drinks & beverages", rules=[
        # Mocktail and dessert syrups are food, not the pharma 'syrups' node.
        Rule("beverage syrup — food, not a formulation", NO_EQ, NO_EQ, 0.90,
             subcats=("syrups",)),
        Rule("coconut water", NO_EQ, NO_EQ, 0.92, subcats=("coconut water",)),
        Rule("meal replacement shake", OTX_FF, "QUISTA", 0.78,
             subcats=("meal replacement shake",)),
    ],
    default=(UNRES, UNRES, 0.40, "beverage type not identified"),
)

# ------------------------------------------------------- protein & gainers
PACKS["whey protein"] = CategoryPack(
    name="whey protein", rules=[],
    subcat_map={"mass whey": (OTX_FF, "QUISTA", 0.86),
                "mainstream whey": (OTX_FF, "QUISTA", 0.86),
                "premium whey": (OTX_FF, "QUISTA", 0.86)},
    default=(UNRES, UNRES, 0.40, "protein type not identified"),
)

PACKS["mass gainer"] = CategoryPack(
    name="mass gainer", rules=[],
    subcat_map={"mass and weight gainers": (OTX_FF, "QUISTA", 0.84)},
    default=(UNRES, UNRES, 0.40, "not identified"),
)

PACKS["health drink & mixes"] = CategoryPack(
    name="health drink & mixes", rules=[
        Rule("electrolyte / rehydration", OTX_FF, "RESTORE RECOVERY DRINK", 0.82,
             subcats=("electrolyte supplements",)),
        # INTERIM -- the client is expected to supply the authoritative 1DS ->
        # master category mapping. Replace these two rules with theirs when it
        # lands; they are here so the QUISTA cluster is not blocked meanwhile.
        #
        # The QUISTA line is split across TWO master categories, and the split
        # is decided by the variant word in the title:
        #   QUISTA PRO / PRO MASS            -> OTX - FUNCTIONAL FOODS  (8 rows)
        #   QUISTA KIDZ / DN / ACTIVE / MOMZ -> PHARMA - OTHERS        (29 rows)
        # PHARMA - OTHERS / QUISTA also holds the 21 HIOWNA rows -- the same
        # paediatric/adult nutrition line under its older brand name.
        #
        # Routing every "powdered drink mixes" listing to OTX_FF put the correct
        # row OUTSIDE the category gate for every KIDZ/DN/ACTIVE listing, so it
        # was filtered out before scoring ran and no scoring change could reach
        # it. Measured on 7 steward-reviewed SKUs (B07R9DYYCG, B089FGW1SK,
        # B0CPXYF4F2, B0CPXZ259S, B0CPXZBY9D, B0CPY1RWZR, B0D9M95DWH): every one
        # names its variant explicitly in the title, and every one wants a
        # PHARMA - OTHERS row. The gate went 0/7 to 7/7, and 4 of 7 then rank
        # the expected row #1.
        #
        # The other 3 are NOT mapping problems and this rule cannot fix them:
        # 7004354 and 7004355 are duplicate master rows with identical titles
        # (the engine picks the other one), and the two Kidz listings compete
        # with HIOWNA KIDZ rows and refill packs in the same category -- one of
        # them is a 2x200g combo with no matching master row at all.
        #
        # Ordered before the generic rule below; first match wins.
        Rule("quista pharma variant", PH_O, "QUISTA", 0.88,
             title=r"\bquista\b.{0,20}?\b(kidz|dn|active|momz)\b|"
                   r"\b(kidz|dn|active|momz)\b.{0,20}?\bquista\b",
             subcats=("powdered drink mixes",)),
        Rule("hiowna", PH_O, "QUISTA", 0.88,
             title=r"\bhiowna\b",
             subcats=("powdered drink mixes",)),
        Rule("powdered drink mix", OTX_FF, "QUISTA", 0.80,
             subcats=("powdered drink mixes",)),
    ],
    default=(UNRES, UNRES, 0.40, "not identified"),
)

PACKS["coffee & tea"] = CategoryPack(
    name="coffee & tea", rules=[],
    subcat_map={"green tea": (OTX_FF, "TEA", 0.92)},
    default=(UNRES, UNRES, 0.40, "not identified"),
)

# ------------------------------------------------------------- eye & other
PACKS["eye makeup"] = CategoryPack(
    name="eye makeup", rules=[],
    subcat_map={"kajal & kohls": (FACE_OTH, "KAJAL", 0.93)},
    default=(UNRES, UNRES, 0.40, "not identified"),
)

PACKS["eye care"] = CategoryPack(
    name="eye care", rules=[
        Rule("eye drops", PH_F, "OPTHACARE", 0.88,
             title=r"\beye drops?\b|\bophthalmic\b|\beye lubricant\b"),
    ],
    default=(UNRES, UNRES, 0.45, "eye care, form not identified"),
)

PACKS["household supplies"] = CategoryPack(
    name="household supplies", rules=[],
    subcat_map={"hand sanitisers": (PERS, "HAND SANITIZER", 0.93)},
    default=(UNRES, UNRES, 0.40, "not identified"),
)

PACKS["beard care"] = CategoryPack(
    name="beard care", rules=[
        Rule("shaving product", MENS, "SHAVING", 0.88,
             title=r"\bshav\w*\b|\brazor\b|\baftershave\b"),
        Rule("beard oil", MENS, "BEARD OIL", 0.88,
             title=r"\bbeard oil\b|\bbeard serum\b|\bgrowth oil\b"),
        # V2 sent all four of these to NO MASTER EQUIVALENT. Two of them are
        # wrong against this master: "beard wash" and "beard comb" both hit
        # MEN FACE & BEARD WASH (7004729, 7004568, and 10 more kit SKUs),
        # filed under MENS CARE / FACE WASH. Only beard shampoo and beard
        # wax are genuinely absent. Since NO_EQ is terminal, the combined
        # rule closed beard-wash rows before retrieval could find them.
        Rule("beard wash", MENS, "FACE WASH", 0.78,
             title=r"\bbeard wash\b|\bbeard comb\b"),
        Rule("beard shampoo / wax — not in the Himalaya range", NO_EQ, NO_EQ, 0.78,
             title=r"\bbeard shampoo\b|\bbeard wax\b"),
    ],
    default=(MENS, "BEARD OIL", 0.62, "beard care, specific form not stated"),
)

PACKS["health & wellness"] = CategoryPack(
    name="health & wellness",
    him_lookup=_scoped(OTX_F, OTX_O, OTX_PS),
    rules=[
        Rule("anti-hangover", OTX_PS, "PARTYSMART", 0.88, subcats=("anti hangover",)),
        Rule("balm format", OTX_O, "BALMS", 0.84, title=r"\bbalm\b"),
        Rule("muscle & joint rub", OTX_O, "MUSCLE & JOINT RUB", 0.80,
             title=r"\brub\b|\bointment\b"),
        Rule("pain relief gel / spray", OTX_F, "RUMALAYA", 0.84,
             subcats=("pain relief creams, gels & sprays",)),
    ],
    default=(UNRES, UNRES, 0.40, "not identified"),
)

PACKS["medication & remedies"] = CategoryPack(
    name="medication & remedies",
    # BABY_TOI is scoped in for the baby rubs: him_lookup already knows
    # 'soothing baby rub' -> BABY TOILETRIES / BABY RUB, but scoping it to
    # (OTX_F, PH_F) filtered that entry out, so a baby rub listed under
    # cough & cold fell through to the generic KOFLET rule below.
    him_lookup=_scoped(OTX_F, PH_F, BABY_TOI),
    rules=[
        # "cough & cold" is a 1DS subcategory name, not a Himalaya dosage
        # form -- it covers Koflet syrup/lozenges AND the unrelated Cold
        # Balm line, which the blanket KOFLET rule below used to swallow
        # whole. "Himalaya Cold Balm" (title says nothing about syrup or
        # lozenges) gated to KOFLET and scored 0.77 against a
        # syrup+balm-combo master row, never even reaching the correct
        # plain COLD BALM 10g (health & wellness's own "balm format" rule,
        # line 424 above, never runs here -- it lives in a different pack
        # this category doesn't consult). Ordered before KOFLET so an
        # unambiguous balm title wins first.
        #
        # not_title excludes bundle language: "Kolfet Syrup+Cold Balm...
        # Free" and "Cold Balm Combo" are genuinely ambiguous about which
        # product is the primary one, and stay on the KOFLET path below
        # rather than this rule guessing which side of the bundle it is.
        # Deliberately NOT \bwith\b alone -- "Cold Balm Rapid Action WITH
        # Eucalyptus" names an ingredient, not a second product, and a bare
        # "with" veto would have excluded it from its own correct rule.
        Rule("cold balm (not a cough syrup/lozenge)", OTX_O, "BALMS", 0.84,
             subcats=("cough & cold",), title=r"\bbalm\b",
             not_title=r"\bcombo\b|\bkit\b|\bfree\b|\+|\bsyrup\b"),
        Rule("cough formulation", OTX_F, "KOFLET", 0.80, subcats=("cough & cold",)),
        # 'syrups' names a dosage form and nothing else. Ten unrelated Himalaya
        # brands ship as syrups, so no category-level rule can resolve it.
        Rule("dosage-form-only node — needs SKU", UNRES, UNRES, 0.35,
             subcats=("syrups",)),
    ],
    default=(UNRES, UNRES, 0.40, "not identified"),
)

PACKS["health supplements"] = CategoryPack(
    name="health supplements",
    him_lookup=_scoped(OTX_F, PH_F),
    rules=[
        Rule("male vitality segment", OTX_F, "TENTEX ROYAL", 0.76,
             subcats=("testosterone boosters",)),
    ],
    default=(UNRES, UNRES, 0.40, "not identified"),
)

PACKS["women hygiene"] = CategoryPack(
    name="women hygiene",
    rules=[
        # V2 shipped this as NO MASTER EQUIVALENT, commented "Himalaya has no
        # intimate wash in the master". That is false against this master,
        # which carries six of them under PERSONAL HYGIENE / MOTHER CARE
        # (7002990 INTIMATE WASH 100ML, 7002991 200ML, 7003571 50ML,
        # 7004299 50ML 10N, 7003611 200ML+WIPES, plus INTIMATE WIPES
        # 7003016/7003017).
        #
        # Because NO_EQ is terminal, the old rule closed every intimate-wash
        # row before retrieval ran -- Himalaya Intima (B0DXL74J7Y) had
        # previously mapped to 7002990 and regressed to NoHimalayaEquivalent.
        # Confidence is deliberately below CATEGORY_GATE_MIN_CONF (0.80): the
        # 1DS sub-category tells us this is an intimate wash, but MOTHER CARE
        # also holds wipes and other lines, so this narrows nothing safely --
        # let V1's scoring pick the SKU.
        Rule("intimate wash", PERS, "MOTHER CARE", 0.75,
             subcats=("intimate washes",)),
    ],
    default=(UNRES, UNRES, 0.40, "not identified"),
)

PACKS["sexual wellness"] = CategoryPack(
    name="sexual wellness", rules=[
        Rule("personal lubricant — not in the Himalaya range", NO_EQ, NO_EQ, 0.86,
             subcats=("lubes",)),
        Rule("topical gel", PH_F, "HIMCOLIN", 0.70, subcats=("gel & spray",)),
    ],
    default=(UNRES, UNRES, 0.40, "not identified"),
)

# ---------------------------------------------------- the four late ports
# lip makeup, pet care, oral healthcare and vitamins & supplements were the
# four categories V2 hand-audited and this module's header called "a later
# port". They had no pack at all, so every row in them skipped the category
# gate entirely: measured, 638 of 3,600 sampled competitor rows and 104 of the
# 793 oneds_master QC misses.
#
# V2's scripts for these four cannot be vendored as they stand -- each calls
# load_master() at import time and derives its constants from the dataframe
# (lip_makeup.py computes CHAP_MAX/TUBE_MIN from a groupby). The thresholds
# those scripts derive are reproduced here as literals, each verified against
# the live master rather than copied on trust:
#
#   LIP CARE / LIP BALM CHAPSTICKS   pack sizes [4.5]                -> CHAP_MAX 4.5
#   LIP CARE / LIP BALM TUBES        pack sizes [5, 6, 10, 12]       -> TUBE_MIN 5.0
#   LIP CARE / LIP BUTTER            pack sizes [10]
#
# What is NOT reproduced is V2's size-threshold branch (size/npack < TUBE_MIN
# -> chapstick, else tube). The declarative Rule matches a title regex and a
# 1DS sub-category; it cannot divide a parsed size by a pack count. Rather
# than widen Rule for one category, the size-ambiguous rows route to LIP CARE
# at a confidence BELOW CATEGORY_GATE_MIN_CONF (0.80) -- the gate then leaves
# the candidate pool alone and V1's scoring picks the SKU, which is exactly
# what it did before these packs existed. The gain is that the clearly-formed
# rows (butter, tube words, stick words) now gate correctly.

PACKS["lip makeup"] = CategoryPack(
    name="lip makeup",
    him_lookup=_scoped(LIP),
    rules=[
        # Level 1 -- is it lip CARE at all? Himalaya sells balms only, so the
        # colour-cosmetic lines are genuine white space and must not be forced
        # onto a balm. Ordered first: a "tinted lip balm" is still a balm, so
        # the tinted-balm exception is spelled into the veto rather than
        # relying on rule order alone.
        Rule("colour cosmetic, not lip care", NO_EQ, NO_EQ, 0.90,
             title=r"\bliquid lip(stick| colou?r)?\b|\blip ?stick\b|"
                   r"\blip colou?r\b|\blip crayon\b|\blip liner\b|"
                   r"\blip pencil\b|\blip gloss\b|\blip st[ae]in\b|"
                   r"\bmatte lip cream\b|\blip mousse\b|\blip pigment\b|"
                   r"\blip.{0,8}cheek\b|\bmulti ?pot\b|\bsindoor\b|\bbindi\b",
             not_title=r"tinted lip ?balm|lip ?balm.{0,25}tint|"
                       r"tinted.{0,20}balm|colou?r changing lip ?balm"),
        Rule("lip treatment form Himalaya does not sell", NO_EQ, NO_EQ, 0.88,
             title=r"\blip scrub\b|\blip buff\b|\blip (sleeping )?mask\b|"
                   r"\blip oil\b|\blip serum\b|\blip plump"),

        # Level 2 -- which sub-category? Form words are decisive and are read
        # before any size reasoning, exactly as V2 orders them.
        Rule("lip butter", LIP, "LIP BUTTER", 0.95, title=r"\blip butter\b"),
        Rule("tube / jar / pot format", LIP, "LIP BALM TUBES", 0.92,
             title=r"\btube\b|\bjars?\b|\bpots?\b|\btubs?\b|\btins?\b|\bsquee?ze\b"),
        Rule("stick format", LIP, "LIP BALM CHAPSTICKS", 0.93,
             title=r"\bchap ?stick\b|\bbalm stick\b|\bcrayon balm\b|\bcare stick\b",
             not_title=r"\blip ?stick\b"),

        # Level 3 -- a balm with no format word, which is the COMMON case:
        # measured, 22% of Himalaya lip rows and 77% of competitor ones state
        # no size at all, and of those that do the split is roughly even (46%
        # under 5g, 33% at or above). So the sub-category genuinely cannot be
        # decided here and V2 only decided it by dividing a parsed size by a
        # pack count, which Rule cannot express.
        #
        # The CATEGORY is still worth gating: it narrows the pool from the
        # whole master to LIP CARE. The sub-category is a guess, so this names
        # the larger of the two (chapsticks, 46% against 33%) and hands the
        # real choice to V1's scoring -- which sees the parsed pack_value and
        # can separate 4.5g from 10g far better than a regex.
        #
        # Confidence is above CATEGORY_GATE_MIN_CONF deliberately. An earlier
        # draft put it at 0.70 to "stay safe"; measured, that left 65 of 79
        # Himalaya lip rows ungated -- the rule matched and was then discarded,
        # which is the same as having no pack at all.
        Rule("lip balm, format not stated", LIP, "LIP BALM CHAPSTICKS", 0.84,
             title=r"\blip ?balms?\b|\blipbalms?\b|\blips? balms?\b|"
                   r"\blip care\b|\blip therapy\b|\blip bomb\b|"
                   r"\blip moist\w*\b|\blip treatment\b|\blip nourish\w*\b|"
                   r"\blip protect\w*\b|\blip repair\b|\blip conditioner\b|"
                   r"\bpetroleum jelly\b|\baloe lips\b|\blip hydrat\w*\b|"
                   r"\bbaby lips\b|\bballms?\b|\bbalms?\b"),
    ],
    default=(UNRES, UNRES, 0.40, "no lip-care form word"),
)

PACKS["pet care"] = CategoryPack(
    name="pet care",
    him_lookup=_scoped(PET_FOOD, PET_GROOM, PET_SUPP),
    rules=[
        # The 1DS sub-category is the strongest signal here and the three
        # master nodes map onto it almost one-to-one, so these are subcat
        # rules rather than title regexes.
        Rule("pet grooming", PET_GROOM, "ERINA", 0.86,
             subcats=("pet shampoos",)),
        Rule("pet supplement", PET_SUPP, "LIV.52", 0.72,
             subcats=("supplements",)),
        # COMPANION CARE - FOODS splits HEALTHY PET FOOD from HEALTHY TREATS,
        # and the title is the only thing that separates them.
        Rule("pet treat", PET_FOOD, "HEALTHY TREATS", 0.84,
             subcats=("pet food",),
             title=r"\btreats?\b|\bbiscuits?\b|\bcookies?\b|\bchew\b|\bstick\b"),
        Rule("pet food", PET_FOOD, "HEALTHY PET FOOD", 0.86,
             subcats=("pet food",)),
    ],
    default=(UNRES, UNRES, 0.40, "not identified"),
)

PACKS["oral healthcare"] = CategoryPack(
    name="oral healthcare",
    # BABY_TOI is scoped in for kids toothpaste, which the master files
    # under BABY TOILETRIES even when 1DS calls it oral healthcare.
    him_lookup=_scoped(ORAL, BABY_TOI),
    rules=[
        # A brush, floss or irrigator is hardware, not a formulation. V2's
        # oral_healthcare.py gates on this first and so does core.NOT_PRODUCT;
        # repeated here because NOT_PRODUCT only covers part of the vocabulary.
        Rule("oral device, not a formulation", NO_EQ, NO_EQ, 0.90,
             title=r"\btooth ?brush\b|\bfloss\b|\birrigator\b|\bwater ?pik\b|"
                   r"\btongue (cleaner|scraper)\b|\bdenture\b|\bbrush head\b|"
                   r"\bwhitening (strip|pen|kit)\b|\bmouth ?guard\b"),
        # HiOra is Himalaya's THERAPEUTIC oral line and the master files it
        # under PHARMA - FORMULATIONS / HIORA, not ORAL CARE. Measured on the
        # QC sheet: 29 of 150 oral rows (19%) want a pharma-formulations node,
        # and the HiOra listings were the whole of it -- gating them to ORAL
        # CARE removed the correct row from the pool entirely. Ordered before
        # both the mouthwash and toothpaste rules because a HiOra row names
        # its form too ("HiOra-K Toothpaste", "HiOra-K mouthwash").
        Rule("hiora therapeutic oral line", PH_F, "HIORA", 0.90,
             title=r"\bhi ?ora\b"),
        # Oro-T is the second therapeutic oral line and sits in the same place:
        # PHARMA - FORMULATIONS / ORO-T, 3 master rows. Same failure as HiOra
        # on the QC sheet -- an "Oro-T ORAL RINSE" reads as a mouthwash and was
        # gated to ORAL CARE, which does not hold it.
        Rule("oro-t therapeutic oral line", PH_F, "ORO-T", 0.90,
             title=r"\boro ?-? ?t\b"),
        Rule("mouthwash", ORAL, "MOUTHWASH", 0.92, subcats=("mouthwashes",)),
        # BOTANIQUE is Himalaya's own line name -- a competitor toothpaste must
        # not claim it, so it is keyed on the word, not the sub-category.
        # Kids toothpaste is filed under BABY TOILETRIES / KIDS TOOTHPASTE
        # (9 rows), not ORAL CARE / TOOTHPASTE (101). The baby care pack
        # already carries this rule, but a kids toothpaste that arrives as
        # 1DS oral healthcare/toothpastes never reaches that pack, so it
        # gated to the adult node and could not match.
        Rule("kids toothpaste", BABY_TOI, "KIDS TOOTHPASTE", 0.93,
             subcats=("toothpastes",), title=KIDS_CUE),
        Rule("botanique toothpaste", ORAL, "BOTANIQUE TOOTHPASTE", 0.90,
             subcats=("toothpastes",), title=r"\bbotanique\b"),
        Rule("dental cream", ORAL, "DENTAL CREAM", 0.88,
             subcats=("toothpastes",), title=r"\bdental cream\b"),
        Rule("toothpaste", ORAL, "TOOTHPASTE", 0.90, subcats=("toothpastes",)),
    ],
    default=(UNRES, UNRES, 0.40, "not identified"),
)

PACKS["vitamins & supplements"] = CategoryPack(
    name="vitamins & supplements",
    # This category is carried almost entirely by him_lookup, not by rules.
    # The master splits PURE HERBS into 43 sub-categories keyed on the HERB
    # ("AMALAKI", "SHATAVARI - ASPARAGUS", "VRIKSHAMLA - GARCINIA"), which is a
    # lookup, not a rule ladder -- 129 generated phrases already cover it, and
    # hand-writing 43 regexes would be a second, drifting copy of the same
    # table.
    # OTX_F is deliberately NOT scoped in. Its only phrase reachable from this
    # category is VITANATURE - NUTRACEUTICALS, which the live master does not
    # carry -- the four pre-existing packs that do scope it already trip the
    # startup validator on it, and adding a fifth would widen a known gap
    # rather than close one.
    him_lookup=_scoped(OTX_PO, OTX_PORG, PH_PH),
    rules=[
        # Himalaya's range is single-herb ayurvedic. Synthetic vitamins,
        # protein and the sports-nutrition adjacencies are real white space,
        # and 1DS files them under this category in volume (vitamin c 4,155
        # rows, biotin 2,398, fat burners 2,301).
        Rule("synthetic vitamin / mineral — not in the Himalaya range",
             NO_EQ, NO_EQ, 0.86,
             subcats=("vitamin c", "vitamin d", "vitamin b12", "vitamin e",
                      "vitamin b7 (biotin)", "multivitamins", "vitamin gummies",
                      "calcium", "iron", "zinc", "magnesium", "omega 3")),
        Rule("weight / sports supplement — not in the Himalaya range",
             NO_EQ, NO_EQ, 0.84,
             subcats=("fat burners", "pre workout", "creatine", "bcaa",
                      "weight gainers", "meal replacement")),
        Rule("probiotic — not in the Himalaya range", NO_EQ, NO_EQ, 0.82,
             subcats=("acidophilus", "probiotics")),

        # The rest of this category is named after the HERB, and 1DS uses the
        # same names the master does. him_lookup resolves these for Himalaya's
        # own rows, but it is gated behind is_him -- so a competitor "Brahmi
        # tablets" listing got nothing. These route the 1DS sub-category
        # straight to its master node.
        #
        # NOT gated to one node, and that is the whole point. Measured on the
        # 189 QC rows in this category, the reviewer's answers are spread
        # across FOUR master categories:
        #     pharma - pure herbs - others   93  (49%)
        #     otx - pure herbs - others      72  (38%)
        #     otx - formulations             13  ( 7%)
        #     otx - pure herbs - organic      8  ( 4%)
        # The same herb appears on both sides (ASHWAGANDHA under OTX,
        # ASHVAGANDHA under PHARMA), so no rule reading the 1DS name can pick
        # the right one -- an earlier draft sent all of them to PH_PH and would
        # have removed the correct row for 51% of them.
        #
        # So these rows are deliberately left UNGATED: confidence is below
        # CATEGORY_GATE_MIN_CONF (0.80), which records the herb as evidence for
        # a steward and in the judge's prompt while leaving the candidate pool
        # exactly as it was. him_lookup above still resolves the Himalaya rows
        # at 0.97 from a confirmed phrase, which is the only signal here that
        # actually knows which side of the split a herb sits on.
        Rule("single-herb supplement (evidence only, pool left open)",
             PH_PH, "TRIPHALA", 0.55,
             subcats=("neem", "brahmi", "gokshura", "punarnava", "garlic",
                      "bael", "shuddha guggulu", "kapikachhu mucuna",
                      "boswellia serrata", "hadjod", "ashwagandha",
                      "ashvagandha", "triphala", "amalaki", "arjuna",
                      "shatavari", "guduchi", "tulasi", "holy basil",
                      "turmeric", "haridra", "karela", "lasuna", "manjistha",
                      "methi", "fenugreek", "moringa", "shigru", "vasaka",
                      "yashtimadhu", "licorice", "trikatu", "harataki",
                      "haritaki", "ashoka", "tagara", "valerian",
                      "meshashringi", "gymnema", "vrikshamla", "garcinia",
                      "berberine", "mandukaparni", "ginger", "sunthi",
                      "shilajit", "herbals", "sleep supplements")),
    ],
    default=(UNRES, UNRES, 0.40, "not identified"),
)
