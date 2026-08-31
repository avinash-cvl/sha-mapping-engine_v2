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

from rank_bm25 import BM25Okapi

import common.config as C
from common import db
from common.models import MasterProduct, SourceProduct


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
    corpus = [row.text.lower().split() for row in master_rows]
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
