from __future__ import annotations

import argparse
import logging
import uuid

from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
)

from common import db

from oneds_competitor.batch_flow_steps import (
    step_1_source_summary,
    step_2_load_master,
    step_3_build_master_lookup,
    step_4_load_category_mapping,
    step_5_build_eligible_master_groups,
    step_6_get_source_batch,
    step_7_build_bm25_index,
    step_8_search_bm25,
    step_9_prepare_candidates,
    step_10_vector_search,
    step_11_merge_candidates,
    step_12_score_candidates,
    step_13_disposition,
    step_14_llm_judge,
    step_15_persist_results,
    step_16_mark_completed,
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


def determine_mapping_status(match_results):
    """
    Determine mapping status using Rank 1 only.

    Score used:
        max(final_score, ensemble_score)

    Thresholds:
        >= 0.86       -> AutoMatch
        0.61 - 0.85   -> StewardReview
        <= 0.60       -> LowConfidence
    """

    if not match_results:
        return "LowConfidence"

    # Rank 1 only
    rank_one = match_results[0]

    print("========== RANK 1 ==========")

    for attr in dir(rank_one):
        if not attr.startswith("_"):
            try:
                value = getattr(rank_one, attr)
                if not callable(value):
                    print(f"{attr} = {value!r}")
            except Exception:
                pass

    print("============================")

    final_score = getattr(
        rank_one,
        "llm_confidence",
        None,
    )

    print(f"final_score: {final_score}")

    ensemble_score = None

    if getattr(rank_one, "scores", None) is not None:
        ensemble_score = getattr(
            rank_one.scores,
            "ensemble",
            None,
        )

    print(f"ensemble_score: {ensemble_score}")

    # Use the higher of final_score and ensemble_score
    if final_score is None:
        final_score = ensemble_score
    elif (
        ensemble_score is not None
        and ensemble_score > final_score
    ):
        final_score = ensemble_score

    # Determine status
    if final_score is not None and final_score >= 0.86:
        return "AutoMatch"

    if final_score is not None and final_score >= 0.61:
        return "StewardReview"

    if final_score is not None and final_score < 0.61:
        return "LowConfidence"

    return "NoHimalayaEquivalent"

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
):
    """
    Process one source SKU.

    Flow:
        Step 8  - BM25
        Step 9  - Candidate preparation
        Step 10 - Vector search
        Step 11 - Merge candidates
        Step 12 - Score
        Step 13 - Disposition
        Step 14 - LLM Judge

    Steps 15-16 are persisted/updated inside the same worker
    using the worker's dedicated DB connection.
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

    logger.info(
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
        # STEP 8 - BM25
        # ====================================================

        bm25_results = step_8_search_bm25(
            batch=[source],
            bm25_index=bm25_index,
            eligible_master=eligible_master,
            top_n=20,
        )

        logger.info(
            "WORKER | SKU=%r | Step 8 BM25 completed",
            sku,
        )

        # ====================================================
        # STEP 9 - CANDIDATE PREPARATION
        # ====================================================

        candidate_map = step_9_prepare_candidates(
            bm25_results=bm25_results,
        )

        logger.info(
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
        )

        logger.info(
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

        logger.info(
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
        )

        logger.info(
            "WORKER | SKU=%r | Step 12 scoring completed",
            sku,
        )

        # ====================================================
        # STEP 13 - DISPOSITION
        # ====================================================

        disposition_results = step_13_disposition(
            ranked_results=ranked_results,
        )

        logger.info(
            "WORKER | SKU=%r | Step 13 disposition completed",
            sku,
        )

        # ====================================================
        # STEP 14 - LLM JUDGE
        # ====================================================

        final_results = step_14_llm_judge(
            disposition_results=disposition_results,
        )

        # ====================================================
        # STEP 15 - PERSIST RESULTS
        # ====================================================
        persist_summary = None

        if final_results:
            persist_summary = step_15_persist_results(
                conn=worker_conn,
                run_id=run_id,
                final_results=final_results,
                source_table=source_table,
            )

            logger.info(
                "WORKER | SKU=%r | Step 15 persistence completed | %s",
                sku,
                persist_summary,
            )
        else:
            logger.warning(
                "WORKER | SKU=%r | Step 15 skipped | No final results",
                sku,
            )

        # ====================================================
        # STEP 16 - MARK SOURCE COMPLETED
        # ====================================================
        updated_count = 0

        if final_results:
            mapping_status = determine_mapping_status(
                match_results=final_results[
                    source_id
                ]["match_results"]
            )

            updated_count = step_16_mark_completed(
                conn=worker_conn,
                source_table=source_table,
                batch=[source],
                status=mapping_status,
            )

            logger.info(
                "WORKER | SKU=%r | Step 16 completed | updated=%d",
                sku,
                updated_count,
            )
        else:
            logger.warning(
                "WORKER | SKU=%r | Step 16 skipped | No final results",
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
            "persist_summary": persist_summary,
            "updated_count": updated_count,
        }

    except Exception:

        logger.exception(
            "WORKER FAILED | source_id=%s | SKU=%r",
            source_id,
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

    args = parser.parse_args()

    # ========================================================
    # MATCH
    # ========================================================

    if args.command == "match":

        conn = db.get_connection()

        run_id = str(
            uuid.uuid4()
        )

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

                            logger.info(
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

        except Exception:

            logger.exception(
                "BATCH FLOW FAILED | run_id=%s",
                run_id,
            )

            raise

        finally:

            conn.close()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    main()