"""All tunable constants: input files, weights, thresholds, vocab, env config.

Edit this file (or override via env var) before running. Nothing here talks
to a database or an LLM -- it's pure configuration, imported by everything.

load_dotenv() reads sha-pipelines/.env if present (docker-compose doesn't need
it -- it injects env vars directly -- this is for running flow_without_prefect.py
or flow.py straight from a local venv). Path is explicit (not the default
cwd-search) so it's found the same way whether `uv run`/python is invoked
from this directory or anywhere else.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ---------------------------------------------------------------------------
# INPUT FILES
# ---------------------------------------------------------------------------
ONEDS_FILE = os.environ.get("ONEDS_FILE", "data/1ds.xlsx")
NIELSEN_FILE = os.environ.get("NIELSEN_FILE", "data/nielsen.xlsx")
MASTER_FILE = os.environ.get("MASTER_FILE", "data/himalaya_master.xlsx")

# Where checkpoint.py writes each stage's output JSON. Relative paths are
# resolved against the process's cwd -- in Docker this must be set to the
# bind-mounted path (docker-compose.yml sets it to /data/intermediate,
# matching the ../data:/data volume) or files never leave the container.
CHECKPOINT_DIR = os.environ.get("CHECKPOINT_DIR", "data/intermediate")

# ---------------------------------------------------------------------------
# EMBEDDING -- Azure OpenAI only. No local/HuggingFace model: local
# sentence-transformers inference is CPU-heavy and deliberately not used.
# ---------------------------------------------------------------------------
EMBED_DIM = 1536  # text-embedding-3-small; must match the VECTOR(n) column width in 004_sqlserver_master_and_ops.sql
CACHE_DIR = os.environ.get("EMBED_CACHE_DIR", "cache")

# ---------------------------------------------------------------------------
# CANDIDATE GENERATION (Stage 4)
# ---------------------------------------------------------------------------
SEMANTIC_TOPK = 150
LEXICAL_TOPK = 150
TOP_N_OUTPUT = 3
COMPETITOR_TOP_N_OUTPUT = 3

# ---------------------------------------------------------------------------
# SCORE WEIGHTS (Stage 5) -- the AHP reference vector from founding_doc.md
# section B.12. Must sum to ~1.0.
# ---------------------------------------------------------------------------
W_SEMANTIC = 0.30
W_LEXICAL = 0.18
W_CATEGORY = 0.10
W_TYPE = 0.20
W_PACK = 0.10
W_OVERLAP = 0.12

# Multiplicative penalties, applied after the weighted blend above.
DOMAIN_MISMATCH_PENALTY = 0.60
TYPE_HARD_INCOMPAT_PENALTY = 0.40
DIVISION_MISMATCH_PENALTY = 0.85

# ---------------------------------------------------------------------------
# CONFIDENCE TIERS (Stage 6)
# ---------------------------------------------------------------------------
TIER_HIGH = 0.87     # >= -> Matched
TIER_REVIEW = 0.50   # >= -> Medium ; below -> Low
TIER_NOEQ = 0.45     # competitor SKUs below this -> No Himalaya Equivalent

# A row that didn't clear TIER_HIGH can still be promoted to Matched if the
# LLM judge is confident enough -- resolution_method="llm_promoted".
LLM_PROMOTE_SCORE_FLOOR = 0.60
LLM_PROMOTE_CONFIDENCE = 0.75

# ---------------------------------------------------------------------------
# PRODUCT GROUP / PACK TYPE (Stage 5, applied after the weighted blend)
# ---------------------------------------------------------------------------
# The master's own marketing grouping (MasterProduct.product_group) and pack
# type are applied as multiplicative adjustments rather than as new W_*
# weights, deliberately: the W_* vector is the AHP reference from
# founding_doc.md section B.12 and must keep summing to 1.0, so adding a
# seventh weight would mean re-deriving all six. These sit alongside
# DOMAIN_MISMATCH_PENALTY / TYPE_HARD_INCOMPAT_PENALTY instead, which is the
# established pattern for a signal that adjusts rather than averages.
#
# PRODUCT_GROUP_MATCH_BONUS rewards a candidate whose product_group is named
# in the source title -- "Strawberry Shine" separating STRAWBERRY SHINE LIP
# BALM from LITCHI SHINE LIP BALM when both are 4.5g chapsticks and the
# ensemble cannot otherwise tell them apart. A bonus (not a penalty for the
# others) so a listing that names no product group is never pushed down.
#
# PACK_TYPE_MISMATCH_PENALTY is mild by design: pack type is weak evidence
# (a listing rarely states "OFFER SALES PK-MULTI"), so it should only break
# near-ties, never override the blend.
PRODUCT_GROUP_MATCH_BONUS = float(os.environ.get("PRODUCT_GROUP_MATCH_BONUS", "1.08"))
PACK_TYPE_MISMATCH_PENALTY = float(os.environ.get("PACK_TYPE_MISMATCH_PENALTY", "0.97"))

# Applied when BOTH sides state a unit count and the counts disagree -- a
# Pack-of-2 listing against a Pack-of-3 master row. Scaled by how far apart
# they are (see stage_scoring), so 2-vs-3 is nudged and 2-vs-24 is pushed
# hard. Silence on either side is neutral and never penalised: most rows
# state no count at all, and a multipack often carries the SAME PackSize as
# the single, so this is the only signal that can separate them.
PACK_COUNT_MISMATCH_PENALTY = float(os.environ.get("PACK_COUNT_MISMATCH_PENALTY", "0.85"))

# ---------------------------------------------------------------------------
# PRODUCT IDENTITY (oneds_master/stages/stage_identity.py)
# ---------------------------------------------------------------------------
# The ensemble answers "how similar are these two rows"; identity answers a
# different question -- "are these the SAME sellable unit". A row can be the
# same product and still score poorly on similarity: measured, "Himalaya Tan
# Removal Orange Peel Off Mask, 8gm, Pack of 12" against the master's "TAN
# REMOVAL ORANGE PEEL OFF MASK 8G 1X12N SACHET" scored an ensemble of 0.23 --
# below a turmeric face pack at 0.55 -- because the two write the same pack
# three different ways and share few literal tokens. Identity compares the
# PARSED attributes instead, so notation stops mattering.
#
# Every threshold here is a starting point, not a calibrated value. None of
# them has been fitted against reviewed outcomes yet.

# Two sizes count as the same when within this relative tolerance. Not zero:
# the same product is written 4.5g and 4.50g, and 1 KG against 1000 GM must
# agree once normalised. Deliberately tight -- 100ml against 500ml is a
# different sellable unit and must never pass.
IDENTITY_PACK_TOLERANCE = float(os.environ.get("IDENTITY_PACK_TOLERANCE", "0.02"))

# How much each attribute contributes to attribute_score, which is a
# 0-1 measure of "how much of the identity evidence agrees". Weights are
# renormalised over whichever attributes are actually comparable on a given
# pair, so a row that states no variant is not punished for silence.
IDENTITY_W_BRAND = float(os.environ.get("IDENTITY_W_BRAND", "0.15"))
IDENTITY_W_FAMILY = float(os.environ.get("IDENTITY_W_FAMILY", "0.30"))
IDENTITY_W_FORM = float(os.environ.get("IDENTITY_W_FORM", "0.15"))
IDENTITY_W_SIZE = float(os.environ.get("IDENTITY_W_SIZE", "0.25"))
IDENTITY_W_COUNT = float(os.environ.get("IDENTITY_W_COUNT", "0.15"))

# attribute_score at or above which a pair is called a deterministic identity
# match -- provided no critical conflict fired. 0.85 rather than 1.0 because
# the master states some attributes nowhere: a row whose family, form and
# pack all agree should not be denied identity because neither side named a
# variant.
IDENTITY_MATCH_THRESHOLD = float(os.environ.get("IDENTITY_MATCH_THRESHOLD", "0.85"))

# Highest score the engine will ever report. Never 1.0.
#
# 100% is a claim of certainty, and the engine is not certain: it compares
# parsed attributes, and the catalogue does not state every attribute of every
# product. A perfect score on the facts that WERE comparable is not proof that
# the two are the same sellable unit -- packaging variants, e-com-only SKUs
# and jar-versus-blister distinctions routinely differ in ways no title
# mentions. Displaying 1.00 invites a reviewer to trust the row without
# looking, and makes the engine indefensible on the ones it gets wrong.
#
# 0.99 says "as confident as this can get" while leaving the last point of
# doubt visible, which is the honest position.
IDENTITY_MAX_SCORE = float(os.environ.get("IDENTITY_MAX_SCORE", "0.99"))

# Floor for a partially-verified identity match, as a fraction of
# IDENTITY_MAX_SCORE. A pair that agreed on every criterion it could check but
# could only check four of five is strong evidence -- it is not the same claim
# as five of five, and the number a reviewer sees should say so.
#
# Measured on lip makeup, 63 of 77 top-scoring rows had verified only four
# criteria, and the missing one was usually count -- exactly what separates a
# single from a multipack. At 0.80, four-of-five scores 0.95 and five-of-five
# scores 0.99: both still AutoMatch, but no longer indistinguishable.
IDENTITY_PARTIAL_FLOOR = float(os.environ.get("IDENTITY_PARTIAL_FLOOR", "0.80"))

# A pair that trips a critical conflict is multiplied by this and then held
# below the AutoMatch line, whatever the LLM says. The conflict is a fact
# about the products, not an opinion the model is entitled to overrule.
#
# A multiplier rather than a flat cap, because a cap destroyed the ordering it
# was meant to protect: min(blended, 0.60) collapsed every conflicted
# candidate to exactly 0.60, so a 0.92-ensemble near-miss and a 0.73-ensemble
# poor match rendered as the same number in the portal. Scaling keeps them
# distinguishable while still ruling both out of an automatic match.
IDENTITY_CONFLICT_PENALTY = float(os.environ.get("IDENTITY_CONFLICT_PENALTY", "0.65"))

# Hard ceiling for a conflicted pair. Sits below the 0.86 AutoMatch threshold
# so no amount of similarity can carry a contradiction into an auto-match.
IDENTITY_CONFLICT_CAP = float(os.environ.get("IDENTITY_CONFLICT_CAP", "0.85"))

# Which conflicts are treated as critical. Individually switchable so a
# category where one of them is noise can be tuned without disabling the rest.
IDENTITY_CONFLICT_ON_SIZE = os.environ.get("IDENTITY_CONFLICT_ON_SIZE", "true").lower() == "true"
IDENTITY_CONFLICT_ON_COUNT = os.environ.get("IDENTITY_CONFLICT_ON_COUNT", "true").lower() == "true"
# Form conflicts are OFF by default, and that is a statement about the
# vocabulary rather than about product form.
#
# _type_cluster() works off TYPE_VOCABULARY, a hand-maintained list of 86
# terms. Measured against live data it cannot classify 28% of the master or
# 24% of listings at all, and where both sides DO classify it disagrees on 106
# rank-1 pairs -- almost none of which are real form mismatches:
#
#   "Himalaya Herbal Balm Lip 10 g"      vs "LIP BALM 10g"          cream/lip
#   "Himalaya Anti-Hair Fall Cream"      vs "ANTI-HAIR FALL CREAM"   hair/cream
#   "Oil Clear Mud Pack 100gm"           vs "OIL CLEAR MUD FACE PACK" oil/mask
#
# Those are the same product each time. Longest-match-wins picks whichever
# vocabulary term happens to appear, so word order and bundle text decide the
# cluster. Treating that as a hard identity conflict makes the engine's
# correctness depend on a word list nobody can complete -- 25 categories'
# worth of forms, found one screenshot at a time.
#
# Form still contributes to attribute_score, where being wrong costs a little.
# It no longer caps a pair outright, where being wrong costs the match. Size,
# count and family are parsed from the data itself and carry the conflict
# logic instead.
IDENTITY_CONFLICT_ON_FORM = os.environ.get("IDENTITY_CONFLICT_ON_FORM", "false").lower() == "true"
IDENTITY_CONFLICT_ON_BRAND = os.environ.get("IDENTITY_CONFLICT_ON_BRAND", "true").lower() == "true"
# Family is the only attribute that separates two different products sharing a
# form and a pack: a Tan Removal 8g x12 mask and a Dark Spot Turmeric 8g x12
# mask agree on brand, form, size and count. Without this the pair reads as
# near-identity.
IDENTITY_CONFLICT_ON_FAMILY = os.environ.get("IDENTITY_CONFLICT_ON_FAMILY", "true").lower() == "true"

# Family agreement is judged on how much of the master's product line the
# listing actually names, not on an exact subset. Exact matching failed on a
# real pair: "Himalaya Neem Face Wash 100ml" against the group "PURIFYING NEEM
# FACE WASH" missed the single word "purifying" and was called a different
# family -- a marketplace title dropping a catalogue qualifier is not a claim
# about a different product.
#
# At or above MATCH_RATIO the line is considered named; at or below
# CONFLICT_RATIO it is a genuinely different line ("TAN REMOVAL ORANGE" vs
# "DARK SPOT CLEARING TURMERIC" share nothing). Between the two the evidence
# is ambiguous and family is reported as unstated rather than guessed.
IDENTITY_FAMILY_MATCH_RATIO = float(os.environ.get("IDENTITY_FAMILY_MATCH_RATIO", "0.5"))
IDENTITY_FAMILY_CONFLICT_RATIO = float(os.environ.get("IDENTITY_FAMILY_CONFLICT_RATIO", "0.25"))

# Absolute floor alongside the ratio. 251 of the master's 653 product groups
# reduce to exactly TWO distinctive tokens, so a listing that drops one
# qualifier scores precisely 0.50 on them -- and a ratio-only rule leaves a
# third of the catalogue permanently ambiguous. Naming two distinct tokens of
# a product line is strong evidence regardless of how long the line's name is.
IDENTITY_FAMILY_MIN_SHARED = int(os.environ.get("IDENTITY_FAMILY_MIN_SHARED", "2"))

# A product-group token appearing in at most this many groups is treated as a
# VARIANT name, and a listing that does not contain it is a different product.
#
# Measured on the live master: "peach" and "cherry" appear in 2 groups each,
# "litchi" in 1, while "shine" appears in 7 and "neem" in 18. Plain overlap
# scored "Peach Shine Lip Care" as a match for the CHERRY SHINE group, because
# the shared token was "shine" and 1-of-2 cleared the ratio -- the token that
# names the product counted the same as the one that names nothing.
#
# 3 keeps flavour and scent names (2-3 groups) strict while leaving genuinely
# common words to the ratio. Frequencies are learned from the master by
# stage_identity.build_token_frequency(), never hand-listed.
IDENTITY_VARIANT_MAX_GROUPS = int(os.environ.get("IDENTITY_VARIANT_MAX_GROUPS", "3"))

# Catalogue-side packaging vocabulary. These words appear in the master's
# product_group but never in a marketplace listing, so leaving them in the
# distinctive set guarantees zero overlap and a false family conflict.
#
# Measured: master 7000685's group is "REGULAR LIP BALM". "lip" and "balm" are
# generic form words and get stripped, leaving "regular" as the ONLY
# distinctive token -- a word no shopper-facing title contains. The listing
# "Himalaya Herbals Lip Balm, 10G (Pack Of 24)" therefore scored 0/1 overlap
# and was ruled a different product family, which capped a pair that agreed on
# brand, form, size AND count -- an exact 24x10g match the judge scored 0.99.
#
# These describe how a product is packed or sold, never what it is.
IDENTITY_CATALOGUE_WORDS = frozenset(
    part.strip()
    for part in os.environ.get(
        "IDENTITY_CATALOGUE_WORDS",
        "regular,offer,sales,pack,packs,container,carton,display,blister,"
        "jar,bottle,tube,sachet,sachets,refill,combi,combo,kit,free,india,"
        "indian,export,domestic,trade,consumer,institutional",
    ).split(",")
    if part.strip()
)

# Which attributes actually IDENTIFY a product, as opposed to merely being
# consistent with one. Brand is near-constant (the master is Himalaya-only)
# and form is coarse ("shampoo" covers hundreds of SKUs) -- a pair agreeing on
# those two has established almost nothing.
#
# This exists because attribute_score renormalises over whatever was
# comparable, so a listing stating nothing but "himalaya anti dandruff
# shampoo" scored a perfect 1.00 on brand+form alone and was called a
# deterministic match against a 400ml master. Measured against steward
# verdicts, that promoted 8 of 9 known-WRONG matches to the highest tier.
# Identity now requires evidence that actually discriminates.
IDENTITY_DISCRIMINATING = frozenset(
    part.strip()
    for part in os.environ.get("IDENTITY_DISCRIMINATING", "family,size,count").split(",")
    if part.strip()
)

# Minimum comparable attributes before identity can be asserted at all, and
# minimum discriminating ones among them. Two of each: one distinctive
# attribute agreeing is a coincidence away from a different product in the
# same line.
IDENTITY_MIN_COMPARABLE = int(os.environ.get("IDENTITY_MIN_COMPARABLE", "3"))
IDENTITY_MIN_DISCRIMINATING = int(os.environ.get("IDENTITY_MIN_DISCRIMINATING", "2"))

# ---------------------------------------------------------------------------
# V2 SCORING WEIGHTS (final_score) -- opt-in, off by default
# ---------------------------------------------------------------------------
# The existing W_* vector blends six similarity signals into ensemble_score
# and is left exactly as it is. This second vector produces a SEPARATE
# final_score that also weighs the identity evidence and the LLM's verdict,
# neither of which the six can see.
#
# Enabled only when SCORING_V2_ENABLED is true. While it is false the new
# columns are still written for comparison, but disposition keeps using the
# existing logic -- so the two can be measured side by side on the same run
# before anything depends on the new number.
#
# These weights are an initial guess. They are NOT optimal and have not been
# fitted to reviewed outcomes; config.calibration_buckets is where measured
# reliability will eventually come from.
SCORING_V2_ENABLED = os.environ.get("SCORING_V2_ENABLED", "false").lower() == "true"

W2_SEMANTIC = float(os.environ.get("W2_SEMANTIC", "0.20"))
W2_LEXICAL = float(os.environ.get("W2_LEXICAL", "0.10"))
W2_CATEGORY = float(os.environ.get("W2_CATEGORY", "0.05"))
W2_TYPE = float(os.environ.get("W2_TYPE", "0.10"))
W2_PACK = float(os.environ.get("W2_PACK", "0.15"))
W2_OVERLAP = float(os.environ.get("W2_OVERLAP", "0.05"))
W2_ATTRIBUTES = float(os.environ.get("W2_ATTRIBUTES", "0.15"))
W2_LLM = float(os.environ.get("W2_LLM", "0.20"))

# ---------------------------------------------------------------------------
# CATEGORY GATE (oneds_master/category)
# ---------------------------------------------------------------------------
# Minimum category-resolver confidence at which the resolved master
# category/sub-category is trusted enough to gate the candidate pool.
# Below this, the row falls back to the config.oneds_master_category_mapping
# table so recall is never reduced by a weak category decision.
#
# Deliberately NOT TIER_HIGH/TIER_REVIEW: those are product-match thresholds
# and mean something different. The V2 numbers behind this are hand-assigned
# routing constants, not calibrated probabilities -- they gate the candidate
# pool and never enter the ensemble arithmetic.
#
# Set to 1.1 to disable the gate entirely (nothing ever resolves), which is
# the regression lever: a run at 1.1 and a run at the default must agree on
# rank-1 for every row the gate did not fire on.
CATEGORY_GATE_MIN_CONF = float(os.environ.get("CATEGORY_GATE_MIN_CONF", "0.80"))

# Paused for now -- when False, flow.py/flow_without_prefect.py's run_persist()
# never writes to app.*_crosswalk on its own, no matter the confidence tier.
# Every result (including Matched-tier, rank=1 "best match" rows) lands only
# in staging.*_product_mapping for a human steward to approve via the portal.
# Flip back to true (or set AUTO_APPROVE_ENABLED=true) once auto-approval
# should resume.
AUTO_APPROVE_ENABLED = os.environ.get("AUTO_APPROVE_ENABLED", "false").lower() == "true"

# ---------------------------------------------------------------------------
# HIMALAYA BRAND WHITELIST -- controls match_type label only, not matching.
# Lowercased substring match. REVIEW THIS LIST against real data before
# trusting Catalog-Match-vs-Competitive-Substitute labels.
# ---------------------------------------------------------------------------
HIMALAYA_BRANDS = ["Himalaya"]

# ---------------------------------------------------------------------------
# PRODUCT TYPE VOCABULARY -- longest-substring-wins on lower-cased text.
# Multi-word forms first (order matters).
# ---------------------------------------------------------------------------
TYPE_VOCABULARY = [
    "neem face pack", "rinse off mask", "rinse-off mask", "mask rinse off",
    "baby hair oil", "hair nourishment oil", "hair growth oil", "hair care oil",
    "face wash", "face cream", "face serum", "face mask", "face pack",
    "face gel", "face oil", "face toner", "face scrub", "face cleanser",
    "night cream", "day cream", "eye cream", "under eye", "eye serum",
    "body lotion", "body butter", "body wash", "body oil", "body scrub",
    "hair oil", "hair serum", "hair mask", "hair cream", "hair gel",
    "baby massage oil", "massage oil", "baby oil",
    # "lip butter" and "lip mask" are longer forms that must precede the bare
    # "butter"/"mask" below, since longest-match-wins can only pick a term
    # that exists. They were added after a real miss -- "AYURVEDA SECRETS GHEE
    # LIP BUTTER 10G" matched only "butter" and landed in the cream cluster --
    # but adding terms one incident at a time is not a strategy, which is why
    # IDENTITY_CONFLICT_ON_FORM now defaults false rather than relying on this
    # list being complete.
    "lip balm", "lip butter", "lip care", "lip oil", "lip mask", "wet wipes",
    "nursing pads", "breast pads", "bra pads", "nursing pad",
    "gift basket", "gift set", "gift pack", "diaper rash", "baby wipes",
    "talcum powder", "baby powder",
    "facewash", "fcwash", "fcw", "nfp",
    "moisturizer", "moisturiser", "conditioner", "sunscreen", "cleanser",
    "deodorant", "shampoo", "lotion", "serum", "cream", "scrub", "toner",
    "powder", "balm", "spray", "butter", "wipes", "wipe", "soap",
    "mask", "talc", "wash", "gel", "oil", "kit", "deo",
    "diaper", "nappy",
    "toothpaste", "toothgel", "mouthwash",
]

# ---------------------------------------------------------------------------
# PRODUCT TYPE COMPATIBILITY CLUSTERS -- same cluster = no penalty.
# ---------------------------------------------------------------------------
TYPE_CLUSTERS: dict[str, list[str]] = {
    "wash": ["face wash", "facewash", "fcwash", "fcw", "wash", "body wash",
             "cleanser", "face cleanser", "soap", "mouthwash"],
    "cream": ["cream", "face cream", "night cream", "day cream", "eye cream",
              "under eye", "lotion", "body lotion", "moisturizer",
              "moisturiser", "butter", "body butter", "balm"],
    "gel": ["gel", "face gel"],
    "serum": ["serum", "face serum", "eye serum"],
    "mask": ["mask", "face mask", "face pack", "neem face pack",
             "rinse off mask", "rinse-off mask", "mask rinse off", "nfp",
             "scrub", "face scrub", "body scrub"],
    "toner": ["toner", "face toner"],
    "oil": ["oil", "face oil", "body oil", "lip oil", "massage oil",
            "baby massage oil", "baby oil"],
    # "lip oil" is deliberately NOT here -- it already belongs to the oil
    # cluster, and a term in two clusters makes _type_cluster()'s answer
    # depend on dict ordering rather than on the product.
    "lip": ["lip balm", "lip butter", "lip care", "lip mask"],
    "wipes": ["wipes", "wipe", "wet wipes", "baby wipes"],
    "nursing": ["nursing pads", "breast pads", "bra pads", "nursing pad"],
    "gift": ["gift basket", "gift set", "gift pack", "kit"],
    "powder": ["powder", "talc", "talcum powder", "baby powder"],
    "hair": ["shampoo", "conditioner", "hair oil", "baby hair oil",
             "hair nourishment oil", "hair growth oil", "hair care oil",
             "hair serum", "hair mask", "hair cream", "hair gel"],
    "hygiene": ["deodorant", "deo", "spray", "sunscreen"],
    "diaper": ["diaper", "nappy", "diaper rash"],
    "toothpaste": ["toothpaste", "toothgel"],
}

# Hard-incompatible cluster pairs -- heavy penalty regardless of surface score.
TYPE_HARD_INCOMPAT: list[tuple[str, str]] = [
    ("wipes", "nursing"),
    ("mask", "wash"), ("cream", "wash"), ("serum", "wash"),
    ("toner", "wash"), ("gel", "wash"),
    ("cream", "serum"), ("cream", "toner"), ("serum", "toner"),
    ("powder", "mask"),
    ("gift", "wash"), ("gift", "cream"), ("gift", "serum"), ("gift", "gel"),
    ("gift", "mask"), ("gift", "toner"), ("gift", "oil"), ("gift", "lip"),
    ("gift", "powder"),
    ("hair", "wash"), ("hair", "cream"), ("hair", "serum"), ("hair", "gel"),
    ("hair", "mask"), ("hair", "toner"),
    ("diaper", "wash"), ("diaper", "cream"), ("diaper", "serum"),
    ("nursing", "wash"), ("nursing", "cream"), ("nursing", "serum"),
    ("nursing", "gel"), ("nursing", "mask"), ("nursing", "lip"),
    ("powder", "wash"), ("powder", "serum"), ("powder", "gel"),
    ("lip", "wash"), ("lip", "wipes"), ("lip", "nursing"), ("lip", "powder"),
    ("lip", "diaper"), ("lip", "hair"),
    ("toothpaste", "wash"), ("toothpaste", "cream"), ("toothpaste", "serum"),
    ("toothpaste", "mask"), ("toothpaste", "toner"), ("toothpaste", "oil"),
    ("toothpaste", "wipes"), ("toothpaste", "powder"), ("toothpaste", "hair"),
    ("toothpaste", "diaper"), ("toothpaste", "nursing"), ("toothpaste", "gift"),
]

# ---------------------------------------------------------------------------
# SQL SERVER 2025
# ---------------------------------------------------------------------------
SQL_SERVER_DSN = os.environ.get(
    "SQL_SERVER_DSN",
    "Driver={ODBC Driver 18 for SQL Server};"
    "Server=localhost,1433;Database=himalaya_sku_harmonization;"
    "Trusted_Connection=yes;TrustServerCertificate=yes;",
)

# ---------------------------------------------------------------------------
# LLM (LangChain -- see agents/llm_client.py)
# "azure" routes both chat and embeddings through the same Azure OpenAI
# resource -- provider is still swappable to "anthropic"/"openai" via
# LLM_PROVIDER without touching code.
# ---------------------------------------------------------------------------
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "azure")
LLM_MODEL = os.environ.get("LLM_MODEL")  # None -> per-provider default in llm_client.py

# Sampling temperature for every provider. None (the default) means the
# parameter is not sent at all, which is what this pipeline did before the
# setting existed -- so the default changes nothing.
#
# Set LLM_TEMPERATURE=0 for reproducible judging: classification gains
# nothing from sampling variety, and a non-zero temperature makes runs
# irreproducible, which undermines the audit trail. It is opt-in rather than
# hardcoded because reasoning-family models (o*, some gpt-5 deployments)
# reject any non-default temperature with a 400, and the deployment in use
# here is env-driven -- verify your deployment accepts it before enabling.
_LLM_TEMPERATURE_RAW = os.environ.get("LLM_TEMPERATURE")
LLM_TEMPERATURE = (
    float(_LLM_TEMPERATURE_RAW) if _LLM_TEMPERATURE_RAW not in (None, "") else None
)

AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
# AZURE_LLM_DEPLOYMENT = os.environ.get("AZURE_LLM_DEPLOYMENT", "gpt-5.4-mini-1")  # previous mini model -- "gpt-5.4-mini" (no suffix) doesn't exist on this resource -- verified via direct call
AZURE_LLM_DEPLOYMENT = os.environ.get("AZURE_LLM_DEPLOYMENT", "gpt-5-nano")
AZURE_EMBEDDING_DEPLOYMENT = os.environ.get("AZURE_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")

# attribute_fallback/thin_margin_judge calls for one flow run are independent
# per-SKU requests (each gets its own get_chat_model() client, so no shared
# state across threads) -- run them concurrently instead of one at a time.
# Lower this if Azure OpenAI starts returning 429s.
LLM_MAX_WORKERS = int(os.environ.get("LLM_MAX_WORKERS", "12"))

# ---------------------------------------------------------------------------
# Observability (agents/llm_client.py's invoke_and_audit() traces every LLM
# call through this). Token is minted per-project on the SH Observability
# backend -- this one is scoped to project "himalaya-sku", not shared with
# other projects on the same backend.
# ---------------------------------------------------------------------------
OBSERVABILITY_API_URL = os.environ.get("OBSERVABILITY_API_URL", "http://20.11.53.58:8001")
OBSERVABILITY_PROJECT_ID = os.environ.get("OBSERVABILITY_PROJECT_ID", "himalaya-sku")
OBSERVABILITY_API_TOKEN = os.environ.get("OBSERVABILITY_API_TOKEN", "")
OBSERVABILITY_ENABLED = os.environ.get("OBSERVABILITY_ENABLED", "true").lower() == "true"
