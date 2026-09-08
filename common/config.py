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

# How many ranked candidates survive scoring -- this is both what the LLM
# judge gets to choose from and what is persisted for a steward.
#
# Raised from 3 to 5. Three candidates picked by the ensemble is a narrow
# funnel: if the ensemble's ordering is wrong, the right product is not in
# the list and no amount of prompt quality can recover it. Five keeps a
# genuine margin for the judge to reach past a mis-ranked #1 (measured: on
# one face-wash row the judge correctly chose the ensemble's #3) without
# padding the prompt with candidates that are mostly noise.
#
# eval/shortlist_depth.py measures how deep the judge actually reaches, so
# this can be revisited against data rather than intuition.
#
# Note step_15 still persists only the top 3 rows to the mapping table --
# this widens what the JUDGE sees, not what a steward is shown.
TOP_N_OUTPUT = int(os.environ.get("TOP_N_OUTPUT", "5"))
COMPETITOR_TOP_N_OUTPUT = 3

# Ask the LLM judge about every row that has candidates, rather than only
# the thin-margin band stage_disposition.needs_judge() used to gate on.
# See that function for the reasoning. Set to false to restore the old
# behaviour (far fewer LLM calls, decisions made by the ensemble alone).
JUDGE_ALL_CANDIDATES = os.environ.get(
    "JUDGE_ALL_CANDIDATES", "true"
).lower() == "true"

# ---------------------------------------------------------------------------
# SCORE WEIGHTS (Stage 5) -- the AHP reference vector from founding_doc.md
# section B.12. Must sum to ~1.0.
# ---------------------------------------------------------------------------
# W_CATEGORY was 0.10, spent on stage_scoring.category_score() -- a
# substring test the code itself marked "TODO: placeholder", returning a
# flat 1.0/0.3/0.5 on `source_subcategory in master_category`. That is not
# a measurement of anything: the two vocabularies are different taxonomies,
# so it mostly returned 0.3 and acted as a near-constant offset. Its weight
# is moved to W_SEMANTIC, which is now trustworthy -- the master embeddings
# were rebuilt on identity-only text (see eval/reembed_master.py), so
# semantic similarity finally compares like with like.
#
# Category is NOT lost as a signal: the candidate pool is already scoped by
# category upstream (step 5's eligible-master groups), which is a far
# stronger use of the same information than a substring test in the blend.
W_SEMANTIC = 0.40
W_LEXICAL = 0.18
W_TYPE = 0.20
W_PACK = 0.10
W_OVERLAP = 0.12

# ---------------------------------------------------------------------------
# COMPETITOR FLOW WEIGHTS -- oneds_competitor/stages/stage_scoring.py
# ---------------------------------------------------------------------------
# The competitor flow still uses the original six-signal blend, including
# category_score. It is a different matching problem (find a comparable
# RIVAL product, where category similarity is genuinely informative rather
# than a placeholder) and is deliberately out of scope for the Himalaya
# mapping-quality work, so it keeps the exact weight vector it was tuned
# with -- the original 0.30 semantic, not the master flow's raised 0.40.
#
# These are separate constants precisely so that retuning one flow cannot
# silently change the other. Sharing W_SEMANTIC between the two is what made
# the competitor blend briefly sum to 1.1 while this change was in progress.
W_COMPETITOR_SEMANTIC = 0.30
W_COMPETITOR_LEXICAL = 0.18
W_COMPETITOR_CATEGORY = 0.10
W_COMPETITOR_TYPE = 0.20
W_COMPETITOR_PACK = 0.10
W_COMPETITOR_OVERLAP = 0.12

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
    "lip balm", "lip care", "lip oil", "wet wipes",
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
    "lip": ["lip balm", "lip care"],
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
