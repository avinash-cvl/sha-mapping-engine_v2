"""Prefect-free mirror of flow.py, for anyone who can't/won't run a Prefect
server locally. Same stages, same order, same checkpoint.py writes -- just
plain function calls and a CLI instead of @task/@flow/serve().

    python flow_without_prefect.py match
    python flow_without_prefect.py match --oneds-file data/1ds.xlsx --no-llm
    python flow_without_prefect.py resume <run_id>

If flow.py's stage order or persist logic ever changes, mirror the change
here too -- this file intentionally duplicates flow.py's orchestration glue
(not the stage logic itself, which lives in stage_*.py/agents/*.py and is
shared) so it has zero dependency on the `prefect` package.
"""
from __future__ import annotations

import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace

from common import checkpoint
import common.config as C
from common import db
from common import db_models
from common import embedder
import observability_sdk
from oneds_master.stages import stage_attributes
from oneds_master.stages import stage_candidates
from oneds_master.stages import stage_deterministic
from oneds_master.stages import stage_disposition
from oneds_master.stages import stage_ingest
from oneds_master.stages import stage_lexicon
from oneds_master.stages import stage_scoring
from agents import attribute_fallback, mcda_judge, synonyms
from common.models import MasterProduct, MatchResult, ScoreBreakdown, SourceProduct

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("sha_pipelines")


def run_ingest(
    run_id: str, conn: db.Connection, oneds_file: str, master_file: str
) -> tuple[list[SourceProduct], list[MasterProduct]]:
    sources, unmapped_channels = stage_ingest.load_1ds_from_source(conn, run_id, oneds_file)
    master = stage_ingest.load_master_from_source(conn, run_id, master_file)
    if unmapped_channels:
        logger.warning(
            f"{len(unmapped_channels)} 1DS rows had a channel_name not in config.channels "
            f"(landed in raw.oneds_products, excluded from matching): {sorted(set(unmapped_channels))}"
        )
    checkpoint.save(run_id, "stage_ingest__sources", list(sources))
    checkpoint.save(run_id, "stage_ingest__master", list(master))
    return sources, master


def run_deterministic(
    run_id: str,
    conn: db.Connection,
    sources: list[SourceProduct],
    master_by_code: dict[str, MasterProduct],
) -> tuple[list[SourceProduct], list[MatchResult]]:
    approved = stage_deterministic.load_approved_crosswalk(conn, [s.sku for s in sources])
    resolved = [
        stage_deterministic.apply_crosswalk(s, approved[s.sku], master_by_code)
        for s in sources if s.sku in approved
    ]
    remaining = [s for s in sources if s.sku not in approved]
    checkpoint.save(run_id, "stage_deterministic__remaining", list(remaining))
    checkpoint.save(run_id, "stage_deterministic__resolved", list(resolved))
    return remaining, resolved


def run_lexicon(
    run_id: str, sources: list[SourceProduct], save_checkpoint: bool = True
) -> list[SourceProduct]:
    expanded = [stage_lexicon.expand(s) for s in sources]
    if save_checkpoint:
        checkpoint.save(run_id, "stage_lexicon", list(expanded))
    return expanded


def run_attributes(
    run_id: str, sources: list[SourceProduct], use_llm: bool, save_checkpoint: bool = True
) -> tuple[list[SourceProduct], list[dict[str, object]]]:
    updated: list[SourceProduct] = [stage_attributes.extract_attributes(s) for s in sources]
    audit_entries: list[dict[str, object]] = []

    # Only rows the rule-based parser couldn't handle hit the LLM -- each
    # gets its own get_chat_model() client (see attribute_fallback.py), so
    # these are independent requests, safe to fan out across threads.
    fallback_indices = [
        i for i, source in enumerate(updated)
        if use_llm and source.sub_brand is None and source.variant is None
    ]
    if fallback_indices:
        with ThreadPoolExecutor(max_workers=C.LLM_MAX_WORKERS) as executor:
            outcomes = executor.map(
                lambda i: attribute_fallback.extract(updated[i].title, updated[i].brand),
                fallback_indices,
            )
            for i, (sub_brand, variant, audit_entry) in zip(fallback_indices, outcomes):
                audit_entry["sku"] = updated[i].sku
                audit_entries.append(audit_entry)
                updated[i] = replace(updated[i], sub_brand=sub_brand, variant=variant)

    if save_checkpoint:
        checkpoint.save(run_id, "stage_attributes", list(updated))
    return updated, audit_entries


def run_candidates(
    run_id: str,
    conn: db.Connection,
    sources: list[SourceProduct],
    master: list[MasterProduct],
    use_llm: bool,
    synonym_cache: dict[str, list[str]] | None = None,
    save_checkpoint: bool = True,
) -> tuple[dict[str, dict[str, tuple[float, float]]], list[dict[str, object]]]:
    to_embed = stage_candidates.rows_needing_embedding(conn, master)
    if to_embed:
        master_vectors = embedder.embed_many([m.text for m in to_embed], tag="master")
        stage_candidates.write_master_embeddings(conn, to_embed, master_vectors)

    bm25_index = stage_candidates.build_bm25_index(master)

    # Gather representations for all source products
    source_reps_list = [stage_candidates.get_source_representations(s) for s in sources]
    
    # Flatten representations to batch embed
    all_rep_texts: list[str] = []
    rep_counts: list[int] = []
    for reps in source_reps_list:
        texts = list(reps.values())
        all_rep_texts.extend(texts)
        rep_counts.append(len(texts))

    all_rep_vectors = embedder.embed_many(all_rep_texts, tag="source")

    # Unflatten vectors back per source product
    source_vectors_by_rep: list[dict[str, list[float]]] = []
    v_idx = 0
    for reps in source_reps_list:
        rep_vec_map: dict[str, list[float]] = {}
        for rep_key in reps.keys():
            rep_vec_map[rep_key] = all_rep_vectors[v_idx]
            v_idx += 1
        source_vectors_by_rep.append(rep_vec_map)

    # One synonyms.expand() call per unique clean_title
    audit_entries: list[dict[str, object]] = []
    synonym_terms_by_title = synonym_cache if synonym_cache is not None else {}
    if use_llm:
        unique_titles = sorted({s.clean_title for s in sources if s.clean_title not in synonym_terms_by_title})
        if unique_titles:
            source_by_title = {source.clean_title: source for source in sources}
            with ThreadPoolExecutor(max_workers=C.LLM_MAX_WORKERS) as executor:
                outcomes = executor.map(synonyms.expand, unique_titles)
                for title, (terms, audit_entry) in zip(unique_titles, outcomes):
                    synonym_terms_by_title[title] = terms
                    audit_entry["sku"] = source_by_title[title].sku
                    audit_entries.append(audit_entry)

    candidates_by_sku: dict[str, dict[str, tuple[float, float]]] = {}
    for source, reps, rep_vecs in zip(sources, source_reps_list, source_vectors_by_rep):
        semantic_by_rep: dict[str, list[tuple[str, float]]] = {}
        lexical_by_rep: dict[str, list[tuple[str, float]]] = {}

        synonym_terms = synonym_terms_by_title.get(source.clean_title, [])
        syn_suffix = f" {' '.join(synonym_terms)}" if synonym_terms else ""

        for rep_key, rep_text in reps.items():
            vec = rep_vecs[rep_key]
            semantic_by_rep[rep_key] = stage_candidates.semantic_search(conn, vec)
            lexical_query = f"{rep_text}{syn_suffix}"
            lexical_by_rep[rep_key] = stage_candidates.lexical_search(bm25_index, master, lexical_query)

        forced = stage_candidates.forced_phrase_search(source, master)
        candidates_by_sku[source.sku] = stage_candidates.merge_candidates_multi(
            semantic_by_rep, lexical_by_rep, forced
        )

    if save_checkpoint:
        checkpoint.save(run_id, "stage_candidates", [
            {"sku": sku, "candidates": cands} for sku, cands in candidates_by_sku.items()
        ])
    return candidates_by_sku, audit_entries


def run_scoring(
    run_id: str,
    sources: list[SourceProduct],
    candidates_by_sku: dict[str, dict[str, tuple[float, float]]],
    master_by_code: dict[str, MasterProduct],
    save_checkpoint: bool = True,
) -> dict[str, list[tuple[MasterProduct, ScoreBreakdown]]]:
    ranked_by_sku = {
        source.sku: stage_scoring.rank_candidates(source, candidates_by_sku.get(source.sku, {}), master_by_code)
        for source in sources
    }
    if save_checkpoint:
        checkpoint.save(run_id, "stage_scoring", [
            {
                "sku": sku,
                "ranked": [{"product_code": m.product_code, "ensemble": s.ensemble} for m, s in ranked],
            }
            for sku, ranked in ranked_by_sku.items()
        ])
    return ranked_by_sku


def run_disposition(
    run_id: str,
    sources: list[SourceProduct],
    ranked_by_sku: dict[str, list[tuple[MasterProduct, ScoreBreakdown]]],
    use_llm: bool,
    save_checkpoint: bool = True,
) -> tuple[list[MatchResult], list[dict[str, object]]]:
    audit_entries: list[dict[str, object]] = []

    # Only thin-margin rows hit the judge -- each gets its own
    # get_chat_model() client (see mcda_judge.py), so these are
    # independent requests, safe to fan out across threads.
    to_judge = [
        source for source in sources
        if use_llm and stage_disposition.needs_judge(ranked_by_sku.get(source.sku, []))
    ]
    judge_outcomes: dict[str, tuple[str | None, float, str | None]] = {}
    if to_judge:
        with ThreadPoolExecutor(max_workers=C.LLM_MAX_WORKERS) as executor:
            outcomes = executor.map(
                lambda source: mcda_judge.judge(source, ranked_by_sku.get(source.sku, [])),
                to_judge,
            )
            for source, (llm_pick, llm_confidence, llm_reason, audit_entry) in zip(to_judge, outcomes):
                audit_entry["sku"] = source.sku
                audit_entries.append(audit_entry)
                judge_outcomes[source.sku] = (llm_pick, llm_confidence, llm_reason)
                logger.info(f"mcda_judge sku={source.sku} pick={llm_pick} confidence={llm_confidence}")

    results: list[MatchResult] = []
    for source in sources:
        ranked = ranked_by_sku.get(source.sku, [])
        llm_pick, llm_confidence, llm_reason = judge_outcomes.get(source.sku, (None, float("nan"), None))
        results.extend(stage_disposition.disposition_all(source, ranked, llm_pick, llm_confidence, llm_reason))
    if save_checkpoint:
        checkpoint.save(run_id, "stage_disposition", list(results))
    return results, audit_entries


def _is_auto_approved(result: MatchResult) -> bool:
    """Gated on C.AUTO_APPROVE_ENABLED (paused for now) -- while paused,
    every result lands only in staging.*_product_mapping for a human
    steward to review/approve, no matter how high its confidence tier."""
    if not C.AUTO_APPROVE_ENABLED:
        return False
    if result.rank != 1 or result.confidence_tier != "Matched":
        return False
    return result.scores is None or result.scores.penalty_applied is None


def run_persist(
    run_id: str,
    conn: db.Connection,
    results: list[MatchResult],
    audit_entries: list[dict[str, object]],
) -> None:
    """Persist each source SKU in its own transaction.

    Each group consists of one rank-1 result and its ranked alternatives.
    This shape also makes checkpoint replay safe: reprocessing a group
    replaces only that SKU's rows for this run, never the rows already
    committed for another SKU.
    """
    by_source: dict[tuple[str, int], list[MatchResult]] = {}
    for result in results:
        if result.source.channel_key:
            key = (result.source.channel_key, result.source.source_row_id)
            by_source.setdefault(key, []).append(result)

    audit_by_sku: dict[str, list[dict[str, object]]] = {}
    for audit_entry in audit_entries:
        sku = audit_entry.get("sku")
        if isinstance(sku, str):
            audit_by_sku.setdefault(sku, []).append(audit_entry)

    auto_approved = 0
    persisted_rows = 0
    for (channel_key, _), sku_results in by_source.items():
        channel_tables = db_models.resolve_channel_tables(conn.engine, channel_key)
        rank_one = next(result for result in sku_results if result.rank == 1)
        mapping_status = _mapping_status(rank_one)
        db.persist_sku_disposition(
            conn,
            channel_tables,
            run_id,
            sku_results,
            mapping_status,
            audit_by_sku.pop(rank_one.source.sku, []),
        )
        persisted_rows += len(sku_results)
        if _is_auto_approved(rank_one):
            auto_approved += 1

    # This should be empty for the matching flow. Keep the previous audit
    # behavior for any caller that supplies an audit entry without a SKU.
    orphaned_audit = [entry for entries in audit_by_sku.values() for entry in entries]
    if orphaned_audit:
        db.write_llm_audit(conn, run_id, orphaned_audit)

    logger.info(
        f"persisted {persisted_rows} mapping rows across {len(by_source)} SKUs, "
        f"{auto_approved} auto-approved to crosswalk"
    )


def _mapping_status(result: MatchResult) -> str:
    """Preserve the existing rank-1 mapping-status thresholds."""
    final_score = _none_if_nan(result.llm_confidence)
    ensemble_score = _none_if_nan(result.scores.ensemble) if result.scores else None

    

    if final_score is None:
        final_score = ensemble_score
    elif ensemble_score is not None and ensemble_score > final_score:
        final_score = ensemble_score

    if final_score is not None and final_score >= 0.86:
        return "AutoMatch"
    if final_score is not None and 0.61 <= final_score <= 0.85:
        return "StewardReview"
    if final_score is not None and final_score <= 0.60:
        return "LowConfidence"
    return "StewardReview"

def _none_if_nan(value: float | None) -> float | None:
    if value is None:
        return None
    return None if value != value else value  # NaN is the only float that != itself


def _match_source_in_worker(
    run_id: str,
    source: SourceProduct,
    master: list[MasterProduct],
    master_by_code: dict[str, MasterProduct],
    use_llm: bool,
) -> tuple[list[MatchResult], list[dict[str, object]]]:
    """Match one SKU using a worker-owned read connection.

    Checkpointing and persistence are deliberately left to the main thread,
    so concurrent workers cannot overwrite a shared checkpoint file or share
    a SQLAlchemy connection.
    """
    worker_conn = db.get_connection()
    try:
        expanded_source = run_lexicon(run_id, [source], save_checkpoint=False)[0]
        attributed_sources, attribute_audit = run_attributes(
            run_id, [expanded_source], use_llm, save_checkpoint=False
        )
        candidates_by_sku, synonyms_audit = run_candidates(
            run_id,
            worker_conn,
            attributed_sources,
            master,
            use_llm,
            synonym_cache={},
            save_checkpoint=False,
        )
        ranked_by_sku = run_scoring(
            run_id, attributed_sources, candidates_by_sku, master_by_code, save_checkpoint=False
        )
        sku_results, disposition_audit = run_disposition(
            run_id, attributed_sources, ranked_by_sku, use_llm, save_checkpoint=False
        )
        return sku_results, attribute_audit + synonyms_audit + disposition_audit
    finally:
        worker_conn.close()


def run_matching_flow(
    oneds_file: str = C.ONEDS_FILE,
    master_file: str = C.MASTER_FILE,
    use_llm: bool = True,
) -> list[MatchResult]:
    conn = db.get_connection()
    run_id = db.create_run(conn, "sku-harmonization-matching", oneds_file)
    logger.info(f"run_id={run_id} oneds_file={oneds_file} master_file={master_file}")

    try:
        sources, master = run_ingest(run_id, conn, oneds_file, master_file)
        master_by_code = {m.product_code: m for m in master}

        sources, crosswalk_results = run_deterministic(run_id, conn, sources, master_by_code)
        logger.info(f"crosswalk short-circuit: {len(crosswalk_results)} rows, {len(sources)} remaining")

        # Write any missing master embeddings before workers start. This
        # prevents workers from attempting to insert the same vectors.
        to_embed = stage_candidates.rows_needing_embedding(conn, master)
        if to_embed:
            master_vectors = embedder.embed_many([m.text for m in to_embed], tag="master")
            stage_candidates.write_master_embeddings(conn, to_embed, master_vectors)

        # Run source SKUs concurrently, but persist each completed future on
        # the main thread immediately. This preserves per-SKU DB updates.
        pipeline_results: list[MatchResult] = []
        all_audit: list[dict[str, object]] = []
        with ThreadPoolExecutor(max_workers=C.LLM_MAX_WORKERS) as executor:
            futures = {
                executor.submit(_match_source_in_worker, run_id, source, master, master_by_code, use_llm): source
                for source in sources
            }
            for future in as_completed(futures):
                source = futures[future]
                sku_results, sku_audit = future.result()

                # Save a growing replay checkpoint before the transaction.
                # Results are persisted as each source worker completes.
                pipeline_results.extend(sku_results)
                all_audit.extend(sku_audit)
                checkpoint.save(run_id, "stage_disposition", list(pipeline_results))
                checkpoint.save(run_id, "stage_persist__llm_audit", list(all_audit))
                run_persist(run_id, conn, sku_results, sku_audit)
                logger.info(f"persisted completed SKU {source.sku}")

        all_results = crosswalk_results + pipeline_results

        db.finish_run(conn, run_id, "completed", len(all_results))
        logger.info(
            f"run complete: {len(all_results)} SKUs resolved, "
            f"{len(all_audit)} LLM calls audited, "
            f"checkpoints under {C.CHECKPOINT_DIR}/{run_id}/"
        )
        return all_results
    except Exception as exc:
        db.finish_run(conn, run_id, "failed", 0, error=str(exc))
        raise
    finally:
        conn.close()


def _match_result_from_checkpoint_dict(d: dict[str, object]) -> MatchResult:
    source_dict = d["source"]
    assert isinstance(source_dict, dict)
    source = SourceProduct(**source_dict)

    candidate_dict = d["candidate"]
    candidate = MasterProduct(**candidate_dict) if isinstance(candidate_dict, dict) else None

    scores_dict = d["scores"]
    scores = ScoreBreakdown(**scores_dict) if isinstance(scores_dict, dict) else None

    return MatchResult(
        source=source,
        candidate=candidate,
        rank=int(d["rank"]),  # type: ignore[call-overload]
        scores=scores,
        confidence_tier=str(d["confidence_tier"]),
        resolution_method=str(d["resolution_method"]),
        llm_pick=d.get("llm_pick"),  # type: ignore[arg-type]
        llm_confidence=float(d.get("llm_confidence", float("nan"))),  # type: ignore[arg-type]
        llm_reason=d.get("llm_reason"),  # type: ignore[arg-type]
    )


def resume_persist_flow(run_id: str) -> None:
    raw_rows = checkpoint.load(run_id, "stage_disposition")
    results = [_match_result_from_checkpoint_dict(row) for row in raw_rows]
    logger.info(f"loaded {len(results)} results from checkpoint for run_id={run_id}")

    conn = db.get_connection()
    try:
        audit_entries: list[dict[str, object]] = []
        if checkpoint.exists(run_id, "stage_persist__llm_audit"):
            audit_entries = checkpoint.load(run_id, "stage_persist__llm_audit")
        run_persist(run_id, conn, results, audit_entries)
        db.finish_run(conn, run_id, "completed", len(results))
        logger.info(f"persist replayed for run_id={run_id}; run marked completed")
    except Exception as exc:
        db.finish_run(conn, run_id, "failed", 0, error=str(exc))
        raise
    finally:
        conn.close()


def main() -> None:
    # observability_sdk.init(
    #     api_url=C.OBSERVABILITY_API_URL,
    #     project_id=C.OBSERVABILITY_PROJECT_ID,
    #     api_token=C.OBSERVABILITY_API_TOKEN,
    #     enabled=C.OBSERVABILITY_ENABLED,
    # )

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    match_parser = subparsers.add_parser("match", help="run the full matching pipeline")
    match_parser.add_argument("--oneds-file", default=C.ONEDS_FILE, help=f"default: {C.ONEDS_FILE}")
    match_parser.add_argument("--master-file", default=C.MASTER_FILE, help=f"default: {C.MASTER_FILE}")
    match_parser.add_argument("--no-llm", action="store_true", help="skip attribute_fallback/synonyms/mcda_judge LLM calls")

    resume_parser = subparsers.add_parser("resume", help="replay persist from a failed run's checkpoint")
    resume_parser.add_argument("run_id", help="the failed run's execution_id (see audit.pipeline_execution_log)")

    args = parser.parse_args()

    if args.command == "match":
        run_matching_flow(args.oneds_file, args.master_file, use_llm=not args.no_llm)
    elif args.command == "resume":
        resume_persist_flow(args.run_id)


if __name__ == "__main__":
    main()
