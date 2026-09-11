"""Stage 4 -- candidate generation (blocking ladder).

Three retrieval channels, merged and de-duplicated:
  1. Semantic -- SQL Server 2025 native vector search (VECTOR_DISTANCE)
     against staging.himalaya_products.embedding. Structured filtering
     (division, category, sap_status) can be added to the SAME query as the
     vector search -- one database, not two services.
  2. Lexical -- BM25 over in-memory master texts (rank_bm25). SQL Server
     full-text search is an alternative but ranks differently; keeping
     BM25 in-process is a deliberate choice, not an oversight.
  3. Forced-phrase rescue -- guarantees a few historically-hard matches
     surface even if channels 1/2 miss them.
"""
from __future__ import annotations

import re

from rank_bm25 import BM25Okapi

import common.config as C
from common import db
from common.models import MasterProduct, SourceProduct

# The pipeline that builds staging.himalaya_products.search_text
# (sha-pipelines/pipelines/staging/processors/himalaya_product_processor.py::
# build_search_text) deduplicates on whitespace-split tokens only, so a
# multipack notation like "2x10g" stays glued as one token while pack_size/
# uom are appended separately as "10 gm" -- neither form contains a bare
# "10g" token. A listing that says "10g (Pack of 2)" then gets a lexical
# score of exactly 0.0 against the one master row that is actually the
# 2-pack, so BM25 starves the correct candidate before scoring ever sees it
# (measured: LIP BALM 2x10g BLISTER, product_code 7000999, ranked #5 behind
# four plain singles it should have beaten).
#
# Fixed here rather than in the ingest pipeline: this runs at BM25-index-build
# time on every batch run, so it applies to already-ingested rows with no
# re-embed, and it is symmetric with parse_pack_count() in stage_attributes.py
# reading the same "NxSIZEunit" shape out of listing titles.
_MULTIPACK_TOKEN_RE = re.compile(r"\b(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)(g|gm|ml|kg|l)\b")

# The mirror notation: SIZE-first, "125gx4N" / "75gx6". The master writes 51
# active rows this way, and BM25 tokenizes "125gx4n" as one unsplittable
# string -- so a listing that says "4x125g" shares NO token with it, while a
# row that happens to spell it "4x125g" scores an exact hit.
#
# Measured on B00YTUG0PG ("Himalaya Herbals Soap - Almond and Rose, 4x125g
# Pack"): the correct 7001720 "ALMOND & ROSE SOAP 125gx4N INDIA VALUE PACK"
# took lexical 0.6235 while the promo row 7003118 "BUY 4X125G & GET 2X75G
# FREE" took 0.8790 -- purely because "4x125g" appears in its text (twice)
# and not in the correct row's. Every other signal was identical between the
# two: overlap 0.5556, pack 1.000, type 1.00, count 4, semantic ~0.80. The
# 0.26 lexical gap alone decided the mapping.
_MULTIPACK_SIZE_FIRST_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(g|gm|ml|kg|l)\s*[x×]\s*(\d{1,4})\s*n?\b",
    re.IGNORECASE,
)


def _expand_multipack_tokens(text: str) -> str:
    """Appends ONE bridge token for "NxSIZEunit" -- the glued unit-size
    ("10g") -- so BM25 can match a bare "10g" query against a "2x10g"
    master row. Additive only: the original text is never altered, so a
    row with no multipack notation is untouched.

    Deliberately narrow. An earlier version also emitted the bare count and
    the split unit/number as separate tokens, which let a WRONG-size
    candidate win: "5g - Pack of 24" matched "LIP BALM 24x10g" (a 10g
    product) at a perfect lexical score, because "24" and "10g" both
    became free-floating tokens and out-scored the correct 5g row's exact
    size but lower lexical hit. Emitting only the glued "10g" form fixes
    the missing-token case without creating a second way to fake a size
    match: pack_score() still sees the true master size (10g vs the
    listing's 5g) and penalizes it, since the size stored in
    MasterProduct.pack_value is untouched by this -- only the BM25 text
    changes.
    """
    extra: list[str] = []
    for _count, size, unit in _MULTIPACK_TOKEN_RE.findall(text):
        extra.append(f"{size}{unit}")   # "10g" -- the common listing form

    # SIZE-first rows get the same two bridges, in the forms a listing writes:
    # the glued unit size ("125g") and the count-first notation ("4x125g").
    #
    # Emitting "4x125g" is safe in a way the rejected bare-count experiment
    # was not: it is a single glued token carrying BOTH the count and the
    # size, so it can only match a listing that states that exact
    # combination. A "5g Pack of 24" listing cannot match "24x10g" through
    # it, because "24x5g" and "24x10g" are different tokens -- which is
    # precisely the failure that made emitting a free-floating "24" wrong.
    for size, unit, count in _MULTIPACK_SIZE_FIRST_RE.findall(text):
        extra.append(f"{size}{unit}")            # "125g"
        extra.append(f"{count}x{size}{unit}")    # "4x125g"

    return f"{text} {' '.join(extra)}" if extra else text


def rows_needing_embedding(conn: db.Connection, master: list[MasterProduct]) -> list[MasterProduct]:
    """Filters out master rows that already have an embedding -- e.g. ones
    read via stage_ingest.load_master_from_staging(), already embedded by
    an earlier ingest/restore. Avoids paying for a re-embed on every run."""
    missing_ids = db.rows_missing_embedding(conn, [row.source_row_id for row in master])
    return [row for row in master if row.source_row_id in missing_ids]


def write_master_embeddings(conn: db.Connection, master: list[MasterProduct], embeddings: list[list[float]]) -> None:
    """Writes each master row's embedding into staging.himalaya_products
    (the row itself was already inserted by stage_ingest.load_master --
    source_row_id is that row's id). Call this once per flow run, before
    the per-SKU candidate loop."""
    for row, vector in zip(master, embeddings):
        db.write_embedding(conn, row.source_row_id, vector)
    conn.commit()


def semantic_search(conn: db.Connection, query_vector: list[float], k: int = C.SEMANTIC_TOPK) -> list[tuple[str, float]]:
    return db.semantic_search(conn, query_vector, k)


def build_bm25_index(master_rows: list[MasterProduct]) -> BM25Okapi:
    corpus = [_expand_multipack_tokens(row.text.lower()).split() for row in master_rows]
    return BM25Okapi(corpus)


def lexical_search(
    index: BM25Okapi,
    master_rows: list[MasterProduct],
    query_text: str,
    k: int = C.LEXICAL_TOPK,
) -> list[tuple[str, float]]:
    scores = index.get_scores(query_text.lower().split())
    top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
    max_score = max(scores) if len(scores) and max(scores) > 0 else 1.0
    return [(master_rows[i].product_code, scores[i] / max_score) for i in top_idx]


def forced_phrase_search(source: SourceProduct, master_rows: list[MasterProduct]) -> list[tuple[str, float]]:
    """Rescue channel for known-hard phrases.

    TODO: port the neem/purifying-style special cases from the prior art's
    forced_phrase.py -- but as config.token_normalization data once
    stage_lexicon.py is real, not as hardcoded phrases in this file.
    """
    return []


def merge_candidates(
    semantic: list[tuple[str, float]],
    lexical: list[tuple[str, float]],
    forced: list[tuple[str, float]],
) -> dict[str, tuple[float, float]]:
    """Union of all three channels, deduplicated by product_code -> (sem, lex)."""
    sem_map = dict(semantic)
    lex_map = dict(lexical)
    for code, score in forced:
        lex_map[code] = max(lex_map.get(code, 0.0), score)
    return {code: (sem_map.get(code, 0.0), lex_map.get(code, 0.0)) for code in set(sem_map) | set(lex_map)}


def get_source_representations(source: SourceProduct) -> dict[str, str]:
    """Generates the 4 text representations for vector search & BM25:
      - title: clean_title
      - title_ing: clean_title + ingredient
      - title_benefit: clean_title + benefit
      - all: clean_title + benefit + ingredient
    """
    reps: dict[str, str] = {"title": source.clean_title}
    if source.ingredient:
        reps["title_ing"] = f"{source.clean_title} {source.ingredient}".strip()
    if source.benefit:
        reps["title_benefit"] = f"{source.clean_title} {source.benefit}".strip()
    if source.ingredient or source.benefit:
        reps["all"] = f"{source.clean_title} {source.benefit or ''} {source.ingredient or ''}".strip()
    return reps


def merge_candidates_multi(
    semantic_by_rep: dict[str, list[tuple[str, float]]],
    lexical_by_rep: dict[str, list[tuple[str, float]]],
    forced: list[tuple[str, float]],
) -> dict[str, tuple[float, float]]:
    """Union of candidates across all text representations, aggregated by max sem and max lex score."""
    sem_max: dict[str, float] = {}
    for rep, sem_list in semantic_by_rep.items():
        for code, score in sem_list:
            sem_max[code] = max(sem_max.get(code, 0.0), score)

    lex_max: dict[str, float] = {}
    for rep, lex_list in lexical_by_rep.items():
        for code, score in lex_list:
            lex_max[code] = max(lex_max.get(code, 0.0), score)

    for code, score in forced:
        lex_max[code] = max(lex_max.get(code, 0.0), score)

    all_codes = set(sem_max) | set(lex_max)
    return {code: (sem_max.get(code, 0.0), lex_max.get(code, 0.0)) for code in all_codes}
