"""
rules.py — one CategoryPack per remaining 1DS category.

Vendored verbatim from V2's `mapping_code/category_rules.py`; the only edit
is the import below. PACKS is the importable form of V2's rules -- the
`categories/*.py` scripts are NOT vendored, because they do
`sys.path.insert(0, "..")` and (for the four pilots) call load_master() at
module import time, so they cannot be used as a library.

PACKS covers 21 of V2's 25 categories. The four hand-audited ones --
lip_makeup, pet_care, oral_healthcare and vitamins_supplements -- carry logic
the declarative model cannot express (pack-size parsing, brand-line tables,
segment mapping) and are a later port, not part of this change. Rows in those
categories get no category gating and fall through to V1's existing
config.oneds_master_category_mapping behaviour untouched; they DO still get
their per-category judge prompt, since agents/prompts/ has all 25.

Rules are ordered; first match wins. Targets are taken verbatim from the
Material Master, and batch_flow.main() asserts at startup that every one
exists (see resolve.pack_targets).

Where a 1DS sub-category has no Himalaya counterpart the pack says so
explicitly with NO HGML EQUIVALENT rather than forcing a near-miss. Those rows
are the portfolio white space and are the most commercially useful output here
— body wash gels, electrolytes and coconut water are all sizeable segments
Himalaya does not currently play in.
"""
from oneds_master.category.core import NO_EQ, UNRES, CategoryPack, Rule

# HGML category constants
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
PH_PH = "PHARMA - PURE HERBS - OTHERS"

ORGANIC = r"\borganic\b|\bcertified organic\b|\busda\b"
MENS_CUE = r"\bfor men\b|\bmen'?s\b|\bmens\b|\bhomme\b|\bbeard\b"
KIDS_CUE = r"\bkids?\b|\bchildren\b|\bbaby\b|\btoddler\b|\bjunior\b"

PACKS = {}

# ---------------------------------------------------------------- face care
PACKS["face care"] = CategoryPack(
    name="face care",
    him_lookup={"clarina": (PH_F, "CLARINA"), "bleminor": (PH_F, "BLEMINOR"),
                "purifying neem": (FACE_CLN, "FACE MASKS")},
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
             subcats=("face wash",), title=MENS_CUE),
        Rule("men's face cream", MENS, "FACE CREAMS", 0.86,
             subcats=("face creams", "night creams"), title=MENS_CUE),
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
        Rule("serum", FACE_SER, "FACE SERUMS", 0.91, subcats=("face serums",)),
        Rule("face wash", FACE_WSH, "FACE WASH", 0.94, subcats=("face wash",)),
        Rule("eye cream -> treatment creams", FACE_MOI, "TREATMENT CREAMS", 0.84,
             subcats=("eye creams",)),
        Rule("night cream -> premium creams", FACE_MOI, "PREMIUM CREAMS", 0.82,
             subcats=("night creams",)),
        Rule("face gel", FACE_MOI, "FACE GELS", 0.86,
             subcats=("face creams",), title=r"\bgel\b", not_title=r"\bcream\b"),
        Rule("face cream", FACE_MOI, "FACE CREAMS", 0.90, subcats=("face creams",)),
        Rule("multi-step kit", FACE_OTH, "FACE CARE - OTHERS", 0.70,
             subcats=("facial kit",)),
    ],
    default=(UNRES, UNRES, 0.40, "face care, form not identified"),
)

# ---------------------------------------------------------------- skin care
PACKS["skin care"] = CategoryPack(
    name="skin care",
    him_lookup={"purely neem": (PERS, "BAR SOAPS")},
    rules=[
        # Himalaya sells bar soap and hand wash, but no shower gel / body wash.
        Rule("body wash / shower gel", NO_EQ, NO_EQ, 0.90,
             subcats=("body wash gels",)),
        Rule("hand sanitiser", PERS, "HAND SANITIZER", 0.93,
             title=r"\bsanitis?er\b|\bsanitizer\b"),
        Rule("hand wash", PERS, "HAND WASH", 0.93, subcats=("hand wash",)),
        Rule("bar soap", PERS, "BAR SOAPS", 0.93, subcats=("soaps",)),
        Rule("sunscreen", SUN, "SUNSCREEN LOTION", 0.88,
             subcats=("sunscreen",), title=r"\blotion\b|\bfluid\b|\bmilk\b"),
        Rule("sunscreen cream", SUN, "SUNSCREEN CREAM", 0.88, subcats=("sunscreen",)),
        Rule("foot care", OTX_O, "FOOT CARE", 0.92, subcats=("foot cream",)),
        Rule("massage oil", OTX_O, "MASSAGE OIL", 0.90, subcats=("massage oils",)),
        Rule("cleansing milk", FACE_CLN, "TONER / MILK", 0.86,
             subcats=("cleansing creams & milks",)),
        Rule("body lotion", BODY_MOI, "BODY LOTIONS", 0.92, subcats=("body lotions",)),
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
    him_lookup={"anti-hair fall": (HAIR, "SHAMPOOS"), "hair zone": (PH_F, "HAIR ZONE")},
    rules=[
        Rule("regrowth serum line", PH_F, "HAIR ZONE", 0.88,
             subcats=("hair regrowth treatments", "hair lotions")),
        Rule("men's hair gel", MENS, "HAIR GELS", 0.90, subcats=("hair gels",)),
        Rule("men's hair cream", MENS, "HAIR CREAMS", 0.86,
             subcats=("hair creams",), title=MENS_CUE),
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
    him_lookup={"baby rub": (BABY_TOI, "BABY RUB")},
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
    him_lookup={"gasex": (OTX_F, "GASEX"), "himcocid": (OTX_F, "HIMCOCID"),
                "liv.52": (PH_F, "LIV.52"), "septilin": (PH_F, "SEPTILIN")},
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
        Rule("beard wash / other grooming", NO_EQ, NO_EQ, 0.78,
             title=r"\bbeard wash\b|\bbeard shampoo\b|\bbeard wax\b|\bbeard comb\b"),
    ],
    default=(MENS, "BEARD OIL", 0.62, "beard care, specific form not stated"),
)

PACKS["health & wellness"] = CategoryPack(
    name="health & wellness",
    him_lookup={"rumalaya": (OTX_F, "RUMALAYA"), "partysmart": (OTX_PS, "PARTYSMART")},
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
    him_lookup={"koflet": (OTX_F, "KOFLET"), "bresol": (PH_F, "BRESOL")},
    rules=[
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
    him_lookup={"tentex royal": (OTX_F, "TENTEX ROYAL"),
                "tentex forte": (PH_F, "TENTEX FORTE"), "confido": (PH_F, "CONFIDO")},
    rules=[
        Rule("male vitality segment", OTX_F, "TENTEX ROYAL", 0.76,
             subcats=("testosterone boosters",)),
    ],
    default=(UNRES, UNRES, 0.40, "not identified"),
)

PACKS["women hygiene"] = CategoryPack(
    name="women hygiene", rules=[
        # Himalaya has no intimate wash in the master.
        Rule("intimate wash — not in the Himalaya range", NO_EQ, NO_EQ, 0.88,
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
