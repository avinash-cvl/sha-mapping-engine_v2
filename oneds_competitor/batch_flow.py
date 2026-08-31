from __future__ import annotations

import argparse
import logging

from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
)

from common import db
from oneds_competitor.stages import stage_disposition

from oneds_competitor.batch_flow_steps import (
    determine_mapping_status,
    step_1_source_summary,
    step_2_load_master,
    step_3_build_master_lookup,
    step_4_load_category_mapping,
    step_5_build_eligible_master_groups,
    step_6_get_source_batch,
    step_6b_apply_crosswalk_shortcircuit,
    step_7_build_bm25_index,
    step_7b_prepare_source_products,
    step_8_search_bm25,
    step_9_prepare_candidates,
    step_10_vector_search,
    step_11_merge_candidates,
    step_12_score_candidates,
    step_13_disposition,
    step_14_llm_judge,
    step_15_add_mapping_data,
    step_16_mark_completed,
)


# ============================================================
# LOGGING
# ============================================================
#
# Configuration is deferred to configure_logging(), called from main()
# once --debug is known (module-level logging.basicConfig() ran before
# argparse, so it couldn't react to CLI flags). Everywhere else in this
# file, logger.info()/.debug()/.error() calls just check the *effective*
# level at call time -- so getLogger(__name__) here is only a reference,
# not the actual configuration.
#
# Verbosity split at production scale (180k SKUs):
#   INFO  -- run start/end, one line per group, one line per completed
#            group's summary (successful/failed counts), all warnings
#            and errors (including every WORKER FAILED).
#   DEBUG -- per-SKU chatter inside steps 8-15 and process_one_sku's
#            per-step "completed" lines. At INFO level these calls
#            short-circuit on a level check before touching the handler
#            lock or formatting anything -- this is what actually fixes
#            the ThreadPoolExecutor lock-contention problem, not just
#            the log volume.

def build_run_log_path(base_log_file: str, run_id: str) -> str:
    """Turns 'batch_flow.log' + a run_id into 'batch_flow_<run_id>.log' --
    each run gets its own file instead of every run appending to/
    overwriting the same one, so a log file maps 1:1 to a row in
    audit.pipeline_execution_log (matched by run_id)."""
    if "." in base_log_file:
        stem, ext = base_log_file.rsplit(".", 1)
        return f"{stem}_{run_id}.{ext}"
    return f"{base_log_file}_{run_id}"


def configure_logging(debug: bool, log_file: str) -> None:
    level = logging.DEBUG if debug else logging.INFO

    handlers: list[logging.Handler] = [logging.FileHandler(log_file)]
    if debug:
        # Also echo to console when debugging interactively -- a real
        # 180k run should go to file only, since console/tty writes have
        # their own overhead on top of the lock contention this whole
        # change is meant to avoid.
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
        handlers=handlers,
        force=True,  # re-configure even if something else already called basicConfig
    )


logger = logging.getLogger(__name__)


# ============================================================
# PROCESS ONE SOURCE SKU
# ============================================================

def process_one_sku(
    source,
    bm25_index,
    eligible_master,
    master_by_code,
    source_table,
    run_id,
    use_llm: bool = True,
    synonym_terms: dict | None = None,
    source_products: dict | None = None,
    pre_audit_entries: list | None = None,
):
    """
    Process one source SKU.

    Flow:
        Step 8  - BM25 (multi-representation: title / title+ingredient /
                  title+benefit / all, synonym terms folded into the
                  lexical query)
        Step 9  - Candidate preparation
        Step 10 - Vector search (same 4 representations)
        Step 11 - Merge candidates
        Step 12 - Score
        Step 13 - Disposition
        Step 14 - LLM Judge

    Step 15 persists the SKU's disposition atomically (mapping rows +
    product status + audit rows in one transaction) using the worker's
    dedicated DB connection. There's no separate Step 16 call for the
    main path any more -- persist_sku_disposition() already updates
    product status as part of that same transaction.

    Lexicon expansion, attribute extraction, and synonym expansion
    (step 7B) already ran once for the whole group in main() before
    workers were spawned -- source_products/synonym_terms are passed in
    rather than recomputed per SKU, so this function doesn't spin up its
    own nested thread pool for a batch of 1. (There's no cross-SKU
    dedup/cache here -- clean_title is effectively unique per SKU in
    this data, so a cache wouldn't save any LLM calls; every SKU still
    gets its own synonyms.expand() call, just made once per group
    instead of once per group AND once per worker.)

    The crosswalk short-circuit (flow.py's stage_deterministic) also
    happens one level up, in main(), before workers are spawned -- SKUs
    already resolved there never reach this function.
    """

    sku = getattr(
        source,
        "sku",
        None,
    )

    source_id = getattr(
        source,
        "id",
        None,
    )

    logger.debug(
        "WORKER START | source_id=%s | SKU=%r",
        source_id,
        sku,
    )

    # --------------------------------------------------------
    # Each worker gets its own DB connection.
    #
    # DO NOT share the main connection between threads.
    # --------------------------------------------------------

    worker_conn = db.get_connection()

    try:

        # ====================================================
        # Lexicon/attribute/synonym prep already ran once for the
        # whole group in main() (step 7B); use that result.
        # Fallback: build it here if this function is ever called
        # standalone without a pre-built source_products dict.
        # ====================================================

        if source_products is None:
            source_products, synonym_terms, attribute_audit = (
                step_7b_prepare_source_products(
                    batch=[source],
                    source_table=source_table,
                    use_llm=use_llm,
                )
            )
        else:
            attribute_audit = []

        # ====================================================
        # STEP 8 - BM25
        # ====================================================

        bm25_results = step_8_search_bm25(
            batch=[source],
            bm25_index=bm25_index,
            eligible_master=eligible_master,
            top_n=20,
            source_products=source_products,
            synonym_terms=synonym_terms,
        )

        logger.debug(
            "WORKER | SKU=%r | Step 8 BM25 completed",
            sku,
        )

        # ====================================================
        # STEP 9 - CANDIDATE PREPARATION
        # ====================================================

        candidate_map = step_9_prepare_candidates(
            bm25_results=bm25_results,
        )

        logger.debug(
            "WORKER | SKU=%r | Step 9 candidate preparation completed",
            sku,
        )

        # ====================================================
        # STEP 10 - VECTOR SEARCH
        # ====================================================

        vector_results = step_10_vector_search(
            conn=worker_conn,
            batch=[source],
            top_k=20,
            source_products=source_products,
        )

        logger.debug(
            "WORKER | SKU=%r | Step 10 vector search completed",
            sku,
        )

        # ====================================================
        # STEP 11 - MERGE CANDIDATES
        # ====================================================

        merged_candidates = step_11_merge_candidates(
            bm25_results=bm25_results,
            vector_results=vector_results,
            eligible_master=eligible_master,
        )

        logger.debug(
            "WORKER | SKU=%r | Step 11 merge completed",
            sku,
        )

        # ====================================================
        # STEP 12 - SCORE
        # ====================================================

        ranked_results = step_12_score_candidates(
            batch=[source],
            merged_candidates=merged_candidates,
            master_by_code=master_by_code,
            source_table=source_table,
            source_products=source_products,
        )

        logger.debug(
            "WORKER | SKU=%r | Step 12 scoring completed",
            sku,
        )

        # ====================================================
        # STEP 13 - DISPOSITION
        # ====================================================

        disposition_results = step_13_disposition(
            ranked_results=ranked_results,
        )

        logger.debug(
            "WORKER | SKU=%r | Step 13 disposition completed",
            sku,
        )

        # ====================================================
        # STEP 14 - LLM JUDGE (gated on use_llm)
        # ====================================================

        if use_llm:
            final_results = step_14_llm_judge(
                disposition_results=disposition_results,
            )
        else:
            # --no-llm: keep each SKU's preliminary (pipeline-only)
            # disposition as the final result, same as flow.py's
            # use_llm=False path -- mcda_judge.judge is never called.
            final_results = {}
            for sid, result in disposition_results.items():
                src = result["source"]
                ranked = result["ranked"]
                match_results = stage_disposition.disposition_all(
                    src, ranked, llm_pick=None,
                    llm_confidence=float("nan"), llm_reason=None,
                )
                final_results[sid] = {
                    **result,
                    "llm_ran": False,
                    "llm_pick": None,
                    "llm_confidence": float("nan"),
                    "llm_reason": None,
                    "match_results": match_results[:3],
                    "audit_entry": None,
                }

        # Fold in any pre-candidate-generation LLM audit entries: the
        # batch-level ones from main() (attribute_fallback + synonyms,
        # step 7B run once per group) plus this function's own local
        # fallback ones if source_products had to be built locally
        # (standalone-call path only).
        combined_pre_audit = list(pre_audit_entries or []) + list(attribute_audit or [])
        if combined_pre_audit and source_id in final_results:
            final_results[source_id]["pre_audit_entries"] = combined_pre_audit

        # ====================================================
        # STEP 15 - ADD MAPPING DATA (atomic per SKU; writes rank
        # 1/2/3 candidate rows, flips mapping_status PENDING ->
        # AutoMatch/StewardReview/LowConfidence/NoHimalayaEquivalent,
        # and writes LLM audit rows, all in one transaction -- see
        # db.persist_sku_disposition -- so there's no separate Step
        # 16 call needed for this path)
        # ====================================================
        mapping_summary = None

        if final_results:
            mapping_summary = step_15_add_mapping_data(
                conn=worker_conn,
                run_id=run_id,
                final_results=final_results,
                source_table=source_table,
            )

            logger.debug(
                "WORKER | SKU=%r | Step 15 mapping data added | %s",
                sku,
                mapping_summary,
            )
        else:
            logger.warning(
                "WORKER | SKU=%r | Step 15 skipped | No final results",
                sku,
            )

        logger.info(
            "WORKER END | source_id=%s | SKU=%r",
            source_id,
            sku,
        )

        return {
            "source": source,
            "results": final_results,
            "mapping_summary": mapping_summary,
        }

    except Exception:

        logger.exception(
            "WORKER FAILED | source_id=%s | SKU=%r",
            source_id,
            sku,
        )

        # Mark this SKU Failed so it's visible instead of sitting in
        # PENDING indefinitely with only a log line to show for it.
        # NOTE: this means step 6 won't pick it up again on a plain
        # re-run of `match` -- it only pulls PENDING rows -- so a
        # Failed SKU needs an explicit re-run (e.g. re-flip it to
        # PENDING) rather than retrying automatically.
        try:
            worker_conn.rollback()
            step_16_mark_completed(
                conn=worker_conn,
                source_table=source_table,
                batch=[source],
                status="Failed",
            )
        except Exception:
            logger.exception(
                "WORKER | SKU=%r | failed to mark source Failed "
                "after the original error above",
                sku,
            )

        raise

    finally:

        worker_conn.close()


# ============================================================
# MAIN
# ============================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description="SKU Harmonization Batch Flow"
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    # ========================================================
    # MATCH COMMAND
    # ========================================================

    match_parser = subparsers.add_parser(
        "match",
        help="Run batch matching flow",
    )

    match_parser.add_argument(
        "--source-table",
        required=True,
        help="Source table, e.g. staging.zepto_products",
    )

    match_parser.add_argument(
        "--master-table",
        required=True,
        help="Master table, e.g. staging.himalaya_products",
    )

    match_parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Maximum source records per category/subcategory batch",
    )

    match_parser.add_argument(
        "--max-workers",
        type=int,
        default=10,
        help="Maximum parallel SKU workers",
    )

    match_parser.add_argument(
        "--category",
        default=None,
        help="Optional source/1DS category filter. If omitted, all categories are processed.",
    )

    match_parser.add_argument(
        "--subcategory",
        default=None,
        help="Optional source/1DS subcategory filter. If omitted, all subcategories are processed.",
    )

    match_parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip attribute_fallback/synonyms/mcda_judge LLM calls (same meaning as flow.py's --no-llm)",
    )

    match_parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Full per-SKU DEBUG-level tracing (steps 8-15's per-step "
            "chatter). Pair with --category/--subcategory to scope to "
            "one small group -- running --debug across a full 180k-row "
            "job defeats the point and reintroduces log-volume/lock "
            "contention at scale."
        ),
    )

    match_parser.add_argument(
        "--log-file",
        default="batch_flow.log",
        help="Path to write logs to (default: batch_flow.log)",
    )

    args = parser.parse_args()
    use_llm = not getattr(args, "no_llm", False)

    # ========================================================
    # MATCH
    # ========================================================

    if args.command == "match":

        conn = db.get_connection()

        # db.create_run() writes to audit.pipeline_execution_log (same
        # table flow.py's runs show up in) and returns the run_id to use
        # as execution_id/batch_id everywhere below. batch_flow.py used
        # to generate its own uuid4() locally, so its runs never appeared
        # in that audit table at all. run_id has to exist before logging
        # is configured, since the log filename is scoped to it.
        run_id = db.create_run(
            conn, "sku-harmonization-batch", args.source_table
        )

        run_log_file = build_run_log_path(args.log_file, run_id)
        configure_logging(debug=args.debug, log_file=run_log_file)

        # total_processed feeds db.finish_run()'s rows_processed count.
        # No separate checkpoint file is kept: each SKU's mapping_status
        # flips away from PENDING the moment step 15 persists it, so a
        # plain re-run of `match` with the same filters already skips
        # everything that finished -- step 6 only ever pulls PENDING
        # rows. That's the resume mechanism.
        total_processed = 0

        logger.info(
            "=================================================="
        )

        logger.info(
            "STARTING BATCH FLOW"
        )

        logger.info(
            "=================================================="
        )

        logger.info(
            "run_id       = %s",
            run_id,
        )

        logger.info(
            "log_file     = %s",
            run_log_file,
        )

        logger.info(
            "source_table = %s",
            args.source_table,
        )

        logger.info(
            "master_table = %s",
            args.master_table,
        )

        logger.info(
            "batch_size   = %d",
            args.batch_size,
        )

        logger.info(
            "max_workers  = %d",
            args.max_workers,
        )

        logger.info(
            "category     = %r",
            args.category,
        )

        logger.info(
            "subcategory  = %r",
            args.subcategory,
        )

        logger.info(
            "=================================================="
        )

        try:

            # =================================================
            # STEP 1
            # Load source groups / summary
            # =================================================

            groups = step_1_source_summary(
                conn=conn,
                source_table=args.source_table,
                master_table=args.master_table,
                batch_size=args.batch_size,
                max_workers=args.max_workers,
                category=args.category,
                subcategory=args.subcategory,
            )

            logger.info(
                "STEP 1 COMPLETED | groups=%d",
                len(groups),
            )

            # =================================================
            # STEP 2
            # Load ALL master records ONCE
            # =================================================

            master_rows = step_2_load_master(
                conn=conn,
                master_table=args.master_table,
            )

            logger.info(
                "STEP 2 COMPLETED | master_records=%d",
                len(master_rows),
            )

            # =================================================
            # STEP 3
            # Build master lookup
            # =================================================

            (
                master_by_code,
                master_lookup,
            ) = step_3_build_master_lookup(
                master_rows
            )

            logger.info(
                "STEP 3 COMPLETED | master_lookup=%d",
                len(master_by_code),
            )

            # NOTE: master-row embedding backfill is NOT done here.
            # It runs as its own separate upstream job before this flow
            # is invoked, so every master row is assumed to already have
            # an embedding by the time step 10's vector search runs.

            # =================================================
            # STEP 4
            # Load category mapping
            # =================================================

            category_mapping = (
                step_4_load_category_mapping(
                    conn=conn,
                    category=args.category,
                    subcategory=args.subcategory,
                )
            )

            logger.info(
                "STEP 4 COMPLETED | mappings=%d",
                len(category_mapping),
            )

            # =================================================
            # STEP 5
            # Build eligible master groups
            # =================================================

            eligible_groups = (
                step_5_build_eligible_master_groups(
                    groups=groups,
                    category_mapping=category_mapping,
                    master_lookup=master_lookup,
                )
            )

            logger.info(
                "STEP 5 COMPLETED | eligible_groups=%d",
                len(eligible_groups),
            )

            # =================================================
            # PROCESS EACH CATEGORY / SUBCATEGORY GROUP
            # =================================================
            # total_processed is initialized before the try block,
            # above, so the except handler can reference it even if
            # a failure happens before this loop starts.

            for group_key, group_info in (
                eligible_groups.items()
            ):

                eligible_master = group_info.get("eligible_master", [])

                if not eligible_master:
                    logger.warning(
                        "SKIPPING GROUP | group_key=%s | "
                        "category=%s | subcategory=%s | "
                        "ELIGIBLE MASTER RECORDS=0",
                        group_key,
                        group_info.get("category"),
                        group_info.get("subcategory"),
                    )

                    no_match_batch = step_6_get_source_batch(
                        conn=conn,
                        source_table=args.source_table,
                        category=group_info["category"],
                        subcategory=group_info["subcategory"],
                        batch_size=args.batch_size,
                    )

                    if no_match_batch:
                        updated_count = step_16_mark_completed(
                            conn=conn,
                            source_table=args.source_table,
                            batch=no_match_batch,
                            status="NoHimalayaEquivalent",
                        )

                        total_processed += updated_count

                        logger.info(
                            "STEP 16 NoHimalayaEquivalent | No eligible master records | "
                            "group_key=%s | updated=%d",
                            group_key,
                            updated_count,
                        )

                    continue

                logger.info(
                    "=================================================="
                )

                logger.info(
                    "PROCESSING GROUP"
                )

                logger.info(
                    "group_key=%s",
                    group_key,
                )

                logger.info(
                    "category=%s | subcategory=%s",
                    group_info["category"],
                    group_info["subcategory"],
                )

                logger.info(
                    "=================================================="
                )

                # =================================================
                # STEP 6
                # Get up to batch_size PENDING source records
                # =================================================

                batch = step_6_get_source_batch(
                    conn=conn,
                    source_table=args.source_table,
                    category=group_info["category"],
                    subcategory=group_info["subcategory"],
                    batch_size=args.batch_size,
                )

                logger.info(
                    "STEP 6 COMPLETED | "
                    "category=%s | subcategory=%s | "
                    "batch_size=%d",
                    group_info["category"],
                    group_info["subcategory"],
                    len(batch),
                )

                if not batch:

                    logger.info(
                        "No PENDING source records for group=%s",
                        group_key,
                    )

                    continue

                # =================================================
                # STEP 6B
                # Deterministic crosswalk short-circuit -- SKUs
                # already approved/auto_approved at match_rank=1 in
                # any active channel's crosswalk skip retrieval,
                # scoring, and the LLM judge entirely.
                # =================================================

                crosswalk_resolved, batch = step_6b_apply_crosswalk_shortcircuit(
                    conn=conn,
                    source_table=args.source_table,
                    master_by_code=master_by_code,
                    batch=batch,
                )

                if crosswalk_resolved:

                    # A crosswalk-approved match is authoritative -- it
                    # doesn't go through determine_mapping_status()'s score
                    # thresholds (its ensemble_score is whatever was
                    # recorded when a steward/auto-approve first confirmed
                    # it, not a fresh score to re-judge), and it's already
                    # a governed record in the crosswalk table itself --
                    # so unlike the scored path, no mapping/candidate rows
                    # need writing here, just the status flip. One bulk
                    # call for the whole group's resolved set, not a
                    # per-SKU loop.
                    resolved_count = step_16_mark_completed(
                        conn=conn,
                        source_table=args.source_table,
                        batch=[r.source for r in crosswalk_resolved],
                        status="Deterministic",
                    )

                    total_processed += resolved_count

                    logger.info(
                        "STEP 6B COMPLETED | crosswalk_resolved=%d | "
                        "remaining=%d",
                        len(crosswalk_resolved),
                        len(batch),
                    )

                if not batch:

                    logger.info(
                        "All source records in group=%s resolved via "
                        "crosswalk short-circuit",
                        group_key,
                    )

                    continue

                # =================================================
                # STEP 7B
                # Lexicon expansion + attribute extraction + synonym
                # expansion -- run once for the whole group's
                # remaining batch (one thread pool), not per SKU.
                # No cross-SKU cache: clean_title is effectively
                # unique per SKU in this data, so every SKU still
                # gets its own synonyms.expand() call either way --
                # this just avoids spinning up a second thread pool.
                # =================================================

                source_products, synonym_terms, pre_audit_entries = (
                    step_7b_prepare_source_products(
                        batch=batch,
                        source_table=args.source_table,
                        use_llm=use_llm,
                        max_workers=args.max_workers,
                    )
                )

                pre_audit_by_sku: dict[str, list] = {}
                for entry in pre_audit_entries:
                    entry_sku = entry.get("sku")
                    if entry_sku:
                        pre_audit_by_sku.setdefault(entry_sku, []).append(entry)

                logger.info(
                    "STEP 7B COMPLETED | source_products=%d | "
                    "audit_entries=%d",
                    len(source_products),
                    len(pre_audit_entries),
                )

                # =================================================
                # STEP 7
                # Build BM25 ONCE for this group
                # =================================================

                bm25_index = (
                    step_7_build_bm25_index(
                        eligible_master=(
                            group_info[
                                "eligible_master"
                            ]
                        ),
                    )
                )

                logger.info(
                    "STEP 7 COMPLETED | "
                    "BM25 index created once | "
                    "eligible_master=%d",
                    len(
                        group_info[
                            "eligible_master"
                        ]
                    ),
                )

                # =================================================
                # STEPS 8-16
                # PARALLEL SKU PROCESSING
                # =================================================

                logger.info(
                    "=================================================="
                )

                logger.info(
                    "STARTING PARALLEL SKU PROCESSING"
                )

                logger.info(
                    "batch=%d | max_workers=%d",
                    len(batch),
                    args.max_workers,
                )

                logger.info(
                    "=================================================="
                )

                all_final_results = {}

                successful_sources = []

                failed_sources = []

                # -------------------------------------------------
                # ThreadPoolExecutor
                # -------------------------------------------------

                with ThreadPoolExecutor(
                    max_workers=args.max_workers
                ) as executor:

                    futures = {
                        executor.submit(
                            process_one_sku,
                            source,
                            bm25_index,
                            group_info[
                                "eligible_master"
                            ],
                            master_by_code,
                            args.source_table,
                            run_id,
                            use_llm,
                            synonym_terms,
                            source_products,
                            pre_audit_by_sku.get(
                                getattr(source, "sku", None), []
                            ),
                        ): source
                        for source in batch
                    }

                    logger.info(
                        "Submitted %d SKU workers",
                        len(futures),
                    )

                    # -------------------------------------------------
                    # Collect completed workers
                    # -------------------------------------------------

                    for future in as_completed(
                        futures
                    ):

                        source = futures[
                            future
                        ]

                        source_id = getattr(
                            source,
                            "id",
                            None,
                        )

                        sku = getattr(
                            source,
                            "sku",
                            None,
                        )

                        try:

                            worker_output = (
                                future.result()
                            )

                            worker_results = (
                                worker_output[
                                    "results"
                                ]
                            )

                            # -----------------------------------------
                            # Merge this SKU's results
                            # -----------------------------------------

                            all_final_results.update(
                                worker_results
                            )

                            successful_sources.append(
                                source
                            )

                            total_processed += 1

                            logger.debug(
                                "WORKER SUCCESS | "
                                "source_id=%s | SKU=%r | "
                                "completed=%d/%d",
                                source_id,
                                sku,
                                len(
                                    successful_sources
                                ),
                                len(batch),
                            )

                        except Exception as exc:

                            failed_sources.append(
                                source
                            )

                            logger.error(
                                "WORKER FAILED | "
                                "source_id=%s | SKU=%r | "
                                "error=%s",
                                source_id,
                                sku,
                                exc,
                            )

                # =================================================
                # PARALLEL PROCESSING SUMMARY
                # =================================================

                logger.info(
                    "=================================================="
                )

                logger.info(
                    "PARALLEL PROCESSING COMPLETED"
                )

                logger.info(
                    "Total submitted : %d",
                    len(batch),
                )

                logger.info(
                    "Successful      : %d",
                    len(
                        successful_sources
                    ),
                )

                logger.info(
                    "Failed          : %d",
                    len(
                        failed_sources
                    ),
                )

                logger.info(
                    "Final results   : %d",
                    len(
                        all_final_results
                    ),
                )

                logger.info(
                    "=================================================="
                )

                # =================================================
                # STEPS 15-16
                # PERSIST + MARK COMPLETED
                # =================================================
                # These steps now execute inside each ThreadPoolExecutor
                # worker using that worker's dedicated DB connection.
                # =================================================

                logger.info(
                    "STEPS 15-16 COMPLETED INSIDE THREADPOOL | "
                    "successful=%d | failed=%d",
                    len(successful_sources),
                    len(failed_sources),
                )

                # =================================================
                # Failed SKU information
                # =================================================

                if failed_sources:

                    logger.warning(
                        "Some SKUs failed and remain PENDING"
                    )

                    for source in failed_sources:

                        logger.warning(
                            "FAILED SKU | id=%s | sku=%s",
                            getattr(
                                source,
                                "id",
                                None,
                            ),
                            getattr(
                                source,
                                "sku",
                                None,
                            ),
                        )

                logger.info(
                    "GROUP COMPLETED | %s",
                    group_key,
                )

            # =====================================================
            # ALL GROUPS COMPLETED
            # =====================================================

            logger.info(
                "=================================================="
            )

            logger.info(
                "BATCH FLOW COMPLETED"
            )

            logger.info(
                "run_id=%s",
                run_id,
            )

            logger.info(
                "=================================================="
            )

            db.finish_run(conn, run_id, "completed", total_processed)

        except Exception as exc:

            logger.exception(
                "BATCH FLOW FAILED | run_id=%s",
                run_id,
            )

            db.finish_run(
                conn, run_id, "failed", total_processed, error=str(exc)
            )

            raise

        finally:

            conn.close()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()