from __future__ import annotations

import logging

import sqlalchemy as sa

from common import db
from common import db_models
from common.models import MasterProduct
from oneds_competitor.stages import stage_candidates
#from services.embedding_service import get_embedder
from common import embedder
from common.models import SourceProduct
from oneds_competitor.stages import stage_scoring
from oneds_competitor.stages  import stage_disposition
from agents import mcda_judge
from common.models import MatchResult
import sqlalchemy as sa
import common.config as C
from common import db_models

import math

logger = logging.getLogger(__name__)

def get_channel_from_source_table(source_table: str) -> str:
    """
    staging.zepto_products -> zepto
    staging.amazon_products -> amazon
    staging.flipkart_products -> flipkart
    """

    table_name = source_table.split(".", 1)[-1]

    suffix = "_products"

    if table_name.lower().endswith(suffix):
        return table_name[:-len(suffix)].lower()

    return table_name.lower()


# ============================================================
# STEP 1 - SOURCE SUMMARY
# ============================================================

def step_1_source_summary(
    conn: db.Connection,
    source_table: str,
    master_table: str,
    batch_size: int,
    max_workers: int,
    category: str | None = None,
    subcategory: str | None = None,
) -> None:

    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 1")
    logger.info("==========================================")
    logger.info(
        "Filter | category=%r | subcategory=%r",
        category,
        subcategory,
    )

    # --------------------------------------------------------
    # 1. Resolve source table
    # --------------------------------------------------------

    if "." not in source_table:
        raise ValueError(
            "source-table must be in schema.table format, "
            "for example: staging.zepto_products"
        )

    source_schema, source_name = source_table.split(
        ".",
        1,
    )

    source = db_models.get_table(
        conn.engine,
        source_schema,
        source_name,
    )

    # logger.info(
    #     "Source table resolved successfully"
    # )

    # --------------------------------------------------------
    # 2. Get total PENDING source records
    # --------------------------------------------------------

    pending_query = (
        sa.select(
            sa.func.count()
        )
        .select_from(source)
        .where(
            source.c.mapping_status == "PENDING",
            ~source.c.brand.in_(C.HIMALAYA_BRANDS),
        )
    )

    # Apply category/subcategory filters only when supplied.
    if category:
        pending_query = pending_query.where(
            sa.func.lower(sa.func.trim(source.c.category)) == category.strip().lower()
        )

    if subcategory:
        pending_query = pending_query.where(
            sa.func.lower(sa.func.trim(source.c.subcategory)) == subcategory.strip().lower()
        )

    pending_count = conn.execute(
        pending_query
    ).scalar_one()

    logger.info(
        "Total PENDING source records: %d",
        pending_count,
    )

    # --------------------------------------------------------
    # 3. Get distinct category/subcategory groups
    # --------------------------------------------------------

    groups = []

    groups_query = (
        sa.select(
            source.c.category,
            source.c.subcategory,
            sa.func.count().label("record_count"),
        )
        .where(
            source.c.mapping_status == "PENDING",
            ~source.c.brand.in_(C.HIMALAYA_BRANDS),
        )
    )

    # Apply filters only when supplied.
    if category:
        groups_query = groups_query.where(
            sa.func.lower(sa.func.trim(source.c.category)) == category.strip().lower()
        )

    if subcategory:
        groups_query = groups_query.where(
            sa.func.lower(sa.func.trim(source.c.subcategory)) == subcategory.strip().lower()
        )

    rows = conn.execute(
        groups_query
        .group_by(
            source.c.category,
            source.c.subcategory,
        )
        .order_by(
            source.c.category,
            source.c.subcategory,
        )
    ).all()

    for row in rows:
        groups.append(
            {
                "category": row.category or "",
                "subcategory": row.subcategory or "",
                "record_count": row.record_count,
            }
        )

    logger.info(
        "Distinct PENDING category/subcategory groups: %d",
        len(groups),
    )

    return groups

# ============================================================
# STEP 2 - LOAD MASTER DATA
# ============================================================

def step_2_load_master(
    conn: db.Connection,
    master_table: str,
):
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 2")
    logger.info("==========================================")

    logger.info(
        "Master table: %s",
        master_table,
    )

    # --------------------------------------------------------
    # 1. Validate master table
    # --------------------------------------------------------

    if "." not in master_table:
        raise ValueError(
            "master-table must be in schema.table format, "
            "for example: staging.himalaya_products"
        )

    master_schema, master_name = master_table.split(
        ".",
        1,
    )

    master = db_models.get_table(
        conn.engine,
        master_schema,
        master_name,
    )

    logger.info(
        "Master table resolved successfully"
    )

    # --------------------------------------------------------
    # 2. Load ALL master records
    # --------------------------------------------------------

    rows = conn.execute(
        sa.select(
            master.c.id,
            master.c.product_code,
            master.c.normalized_title,
            master.c.normalized_category,
            master.c.normalized_subcategory,
            master.c.search_text,
            master.c.normalized_uom,
        )
        .where(
            master.c.product_code.is_not(None),
        )
    ).all()

    logger.info(
        "Total master records loaded: %d",
        len(rows),
    )

    # --------------------------------------------------------
    # 3. Display sample records
    # --------------------------------------------------------

    # logger.info("------------------------------------------")
    # logger.info("Top 2 master records:")

    # for index, row in enumerate(
    #     rows[:2],
    #     start=1,
    # ):
    #     logger.info(
    #         "%d. product_code=%r | title=%r | "
    #         "category=%r | subcategory=%r",
    #         index,
    #         row.product_code,
    #         row.normalized_title,
    #         row.normalized_category,
    #         row.normalized_subcategory,
    #     )

    # logger.info("------------------------------------------")

    logger.info(
        "STEP 2 COMPLETED - MASTER LOADED ONCE"
    )

    return rows

# ============================================================
# STEP 3 - BUILD MASTER LOOKUPS
# ============================================================

def step_3_build_master_lookup(
    master_rows,
):
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 3")
    logger.info("==========================================")

    # --------------------------------------------------------
    # 1. Convert database rows to MasterProduct
    # --------------------------------------------------------

    master_products: list[MasterProduct] = []

    for row in master_rows:

        product = MasterProduct(
            product_code=row.product_code,
            product_name=row.normalized_title or "",
            division="",
            category=row.normalized_category or "",
            subcategory=row.normalized_subcategory or "",
            sap_status="",
            blocked_in_sap=False,
            pack_value=None,
            pack_unit=row.normalized_uom,
            text=(
                row.search_text
                or row.normalized_title
                or ""
            ),
            source_row_id=row.id,
        )

        master_products.append(product)

    logger.info(
        "MasterProduct objects created: %d",
        len(master_products),
    )

    # --------------------------------------------------------
    # 2. Build master_by_code
    # --------------------------------------------------------

    master_by_code = {
        product.product_code: product
        for product in master_products
        if product.product_code
    }

    logger.info(
        "master_by_code entries: %d",
        len(master_by_code),
    )

    # --------------------------------------------------------
    # 3. Build category/subcategory lookup
    # --------------------------------------------------------

    master_lookup = {}

    for product in master_products:

        category = (
            product.category
            or ""
        ).strip().lower()

        subcategory = (
            product.subcategory
            or ""
        ).strip().lower()

        key = (
            category,
            subcategory,
        )

        master_lookup.setdefault(
            key,
            [],
        ).append(product)

    # --------------------------------------------------------
    # 4. Log lookup summary
    # --------------------------------------------------------

    logger.info(
        "Distinct master category/subcategory groups: %d",
        len(master_lookup),
    )

    #logger.info("------------------------------------------")

    for index, (
        key,
        products,
    ) in enumerate(
        master_lookup.items(),
        start=1,
    ):

        category, subcategory = key

        # logger.info(
        #     "%d. category=%r | subcategory=%r | "
        #     "master_records=%d",
        #     index,
        #     category,
        #     subcategory,
        #     len(products),
        # )

    #logger.info("------------------------------------------")

    logger.info(
        "STEP 3 COMPLETED - MASTER LOOKUPS READY"
    )

    return (
        master_by_code,
        master_lookup,
    )


# ============================================================
# STEP 4 - LOAD CATEGORY / SUBCATEGORY MAPPING
# ============================================================

def step_4_load_category_mapping(
    conn: db.Connection,
    category: str | None = None,
    subcategory: str | None = None,
):
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 4")
    logger.info("==========================================")
    logger.info(
        "Filter | category=%r | subcategory=%r",
        category,
        subcategory,
    )

    # --------------------------------------------------------
    # 1. Resolve mapping table
    # --------------------------------------------------------

    mapping_table = db_models.get_table(
        conn.engine,
        "config",
        "oneds_master_category_mapping",
    )

    logger.info(
        "Mapping table resolved: "
        "config.oneds_master_category_mapping"
    )

    # --------------------------------------------------------
    # 2. Load active mappings
    # --------------------------------------------------------

    mapping_query = (
        sa.select(
            mapping_table.c.id,
            mapping_table.c.master_category,
            mapping_table.c.master_subcategory,
            mapping_table.c.oneds_category,
            mapping_table.c.oneds_subcategory,
            mapping_table.c.match_quality,
            mapping_table.c.is_active,
        )
        .where(
            mapping_table.c.is_active == True
        )
    )

    # Apply filters only when supplied.
    if category:
        mapping_query = mapping_query.where(
            sa.func.lower(sa.func.trim(mapping_table.c.oneds_category))
            == category.strip().lower()
        )

    if subcategory:
        mapping_query = mapping_query.where(
            sa.func.lower(sa.func.trim(mapping_table.c.oneds_subcategory))
            == subcategory.strip().lower()
        )

    rows = conn.execute(
        mapping_query
        .order_by(
            mapping_table.c.oneds_category,
            mapping_table.c.oneds_subcategory,
            mapping_table.c.id,
        )
    ).all()

    logger.info(
        "Active category mapping records loaded: %d",
        len(rows),
    )

    # --------------------------------------------------------
    # 3. Build 1:N lookup
    #
    # Key:
    #     ONEDS category + ONEDS subcategory
    #
    # Value:
    #     Multiple master category/subcategory mappings
    # --------------------------------------------------------

    category_mapping = {}

    for row in rows:

        oneds_category = (
            row.oneds_category or ""
        ).strip().lower()

        oneds_subcategory = (
            row.oneds_subcategory or ""
        ).strip().lower()

        master_category = (
            row.master_category or ""
        ).strip().lower()

        master_subcategory = (
            row.master_subcategory or ""
        ).strip().lower()

        source_key = (
            oneds_category,
            oneds_subcategory,
        )

        mapping_record = {
            "id": row.id,
            "master_category": master_category,
            "master_subcategory": master_subcategory,
            "match_quality": (
                row.match_quality or ""
            ).upper(),
        }

        category_mapping.setdefault(
            source_key,
            [],
        ).append(
            mapping_record
        )

    # --------------------------------------------------------
    # 4. Log summary
    # --------------------------------------------------------

    logger.info(
        "Distinct 1DS category/subcategory mappings: %d",
        len(category_mapping),
    )

    logger.info("------------------------------------------")

    for index, (
        source_key,
        mappings,
    ) in enumerate(
        category_mapping.items(),
        start=1,
    ):

        oneds_category, oneds_subcategory = (
            source_key
        )

        logger.info(
            "%d. 1DS=%r / %r -> %d master mappings",
            index,
            oneds_category,
            oneds_subcategory,
            len(mappings),
        )

        for mapping in mappings:

            logger.info(
                "   -> master=%r / %r | quality=%s",
                mapping["master_category"],
                mapping["master_subcategory"],
                mapping["match_quality"],
            )

    logger.info("------------------------------------------")

    logger.info(
        "STEP 4 COMPLETED - CATEGORY MAPPING READY"
    )

    return category_mapping

# ============================================================
# STEP 5 - BUILD ELIGIBLE MASTER GROUPS
# ============================================================

def step_5_build_eligible_master_groups(
    groups,
    category_mapping,
    master_lookup,
):
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 5")
    logger.info("==========================================")

    eligible_groups = {}

    for group in groups:

        oneds_category = (
            group["category"] or ""
        ).strip().lower()

        oneds_subcategory = (
            group["subcategory"] or ""
        ).strip().lower()

        source_key = (
            oneds_category,
            oneds_subcategory,
        )

        logger.info("------------------------------------------")

        logger.info(
            "Source group: category=%r | subcategory=%r",
            oneds_category,
            oneds_subcategory,
        )

        # 1:N mapping
        mappings = category_mapping.get(
            source_key,
            [],
        )

        logger.info(
            "Mapping records found: %d",
            len(mappings),
        )

        eligible_by_code = {}

        for mapping in mappings:

            master_key = (
                mapping["master_category"],
                mapping["master_subcategory"],
            )

            master_rows = master_lookup.get(
                master_key,
                [],
            )

            logger.info(
                "Master group: %r / %r -> %d records",
                master_key[0],
                master_key[1],
                len(master_rows),
            )

            for master in master_rows:

                eligible_by_code[
                    master.product_code
                ] = master

        eligible_master = list(
            eligible_by_code.values()
        )

        eligible_groups[source_key] = {
            "category": oneds_category,
            "subcategory": oneds_subcategory,
            "mappings": mappings,
            "eligible_master": eligible_master,
        }

        logger.info(
            "TOTAL ELIGIBLE MASTER RECORDS: %d",
            len(eligible_master),
        )

    logger.info(
        "STEP 5 COMPLETED - ELIGIBLE MASTER GROUPS READY"
    )

    return eligible_groups


# ============================================================
# STEP 6 - GET SOURCE BATCH
# ============================================================

def step_6_get_source_batch(
    conn: db.Connection,
    source_table: str,
    category: str,
    subcategory: str,
    batch_size: int = 500,
):
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 6")
    logger.info("==========================================")

    logger.info(
        "Source table : %s",
        source_table,
    )

    logger.info(
        "Category     : %r",
        category,
    )

    logger.info(
        "Subcategory  : %r",
        subcategory,
    )

    logger.info(
        "Batch size   : %d",
        batch_size,
    )

    # --------------------------------------------------------
    # 1. Resolve source table
    # --------------------------------------------------------

    if "." not in source_table:
        raise ValueError(
            "source-table must be in schema.table format"
        )

    source_schema, source_name = source_table.split(
        ".",
        1,
    )

    source = db_models.get_table(
        conn.engine,
        source_schema,
        source_name,
    )

    # --------------------------------------------------------
    # 2. Get PENDING source records
    # --------------------------------------------------------

    rows = conn.execute(
        sa.select(
            source
        )
        .where(
            source.c.mapping_status == "PENDING",
            ~source.c.brand.in_(C.HIMALAYA_BRANDS),
        )
        .where(
            source.c.category == category
        )
        .where(
            source.c.subcategory == subcategory
        )
        .order_by(
            source.c.id
        )
        .limit(batch_size)
    ).all()

    logger.info(
        "PENDING records retrieved: %d",
        len(rows),
    )

    # --------------------------------------------------------
    # 3. Display sample
    # --------------------------------------------------------

    logger.info("------------------------------------------")

    for index, row in enumerate(
        rows[:5],
        start=1,
    ):

        logger.info(
            "%d. id=%s | sku=%r | category=%r | "
            "subcategory=%r | title=%r",
            index,
            row.id,
            getattr(row, "sku", None),
            getattr(row, "category", None),
            getattr(row, "subcategory", None),
            getattr(row, "title", None),
        )

    # logger.info("------------------------------------------")

    # logger.info(
    #     "STEP 6 COMPLETED"
    # )

    return rows

# ============================================================
# STEP 7 - BUILD BM25 INDEX
# ============================================================

def step_7_build_bm25_index(
    eligible_master,
):
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 7")
    logger.info("==========================================")

    logger.info(
        "Eligible master records for BM25: %d",
        len(eligible_master),
    )

    if not eligible_master:
        logger.warning(
            "No eligible master records. "
            "BM25 index will not be created."
        )

        return None

    # --------------------------------------------------------
    # Build BM25 index
    # --------------------------------------------------------

    bm25_index = stage_candidates.build_bm25_index(
        eligible_master
    )

    logger.info(
        "BM25 index built successfully"
    )

    logger.info(
        "BM25 corpus size: %d",
        len(eligible_master),
    )

    logger.info(
        "STEP 7 COMPLETED - BM25 INDEX READY"
    )

    return bm25_index

# ============================================================
# STEP 8 - BM25 TOP-N SEARCH PER SOURCE SKU
# ============================================================

def step_8_search_bm25(
    batch,
    bm25_index,
    eligible_master,
    top_n: int = 20,
):
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 8")
    logger.info("==========================================")

    if not bm25_index:
        logger.warning(
            "BM25 index is empty. "
            "Skipping BM25 search."
        )
        return []

    if not batch:
        logger.info(
            "Source batch is empty."
        )
        return []

    logger.info(
        "Source records in batch: %d",
        len(batch),
    )

    logger.info(
        "BM25 Top-N: %d",
        top_n,
    )

    logger.info(
        "Eligible master records: %d",
        len(eligible_master),
    )

    all_results = []

    # --------------------------------------------------------
    # Search separately for every source SKU
    # --------------------------------------------------------

    for source in batch:

        # ----------------------------------------------------
        # Get source title
        # ----------------------------------------------------

        source_title = (
            getattr(
                source,
                "normalized_title",
                None,
            )
            or getattr(
                source,
                "title",
                None,
            )
            or ""
        ).strip()

        if not source_title:

            logger.warning(
                "Source ID=%s has empty title. "
                "Skipping BM25.",
                getattr(source, "id", None),
            )

            all_results.append(
                {
                    "source_id": getattr(
                        source,
                        "id",
                        None,
                    ),
                    "query": "",
                    "candidates": [],
                }
            )

            continue

        # ----------------------------------------------------
        # BM25 SEARCH
        # ----------------------------------------------------

        candidates = stage_candidates.lexical_search(
            bm25_index,
            eligible_master,
            source_title,
            k=top_n,
        )

        # ----------------------------------------------------
        # Store result
        # ----------------------------------------------------

        source_result = {
            "source_id": getattr(
                source,
                "id",
                None,
            ),
            "sku": getattr(
                source,
                "sku",
                None,
            ),
            "query": source_title,
            "candidates": candidates,
        }

        all_results.append(
            source_result
        )

        # ----------------------------------------------------
        # Logging
        # ----------------------------------------------------

        logger.info(
            "Source ID=%s | SKU=%r | "
            "BM25 candidates=%d",
            getattr(source, "id", None),
            getattr(source, "sku", None),
            len(candidates),
        )

        # Show Top 5 only in logs
        # for rank, candidate in enumerate(
        #     candidates[:5],
        #     start=1,
        # ):

        #     logger.info(
        #         "   %d. %s",
        #         rank,
        #         candidate,
        #     )

    # logger.info("------------------------------------------")

    logger.info(
        "BM25 search completed for %d source records",
        len(all_results),
    )

    # logger.info(
    #     "STEP 8 COMPLETED - TOP-N CANDIDATES GENERATED"
    # )

    return all_results

# ============================================================
# STEP 9 - PREPARE CANDIDATES
# ============================================================

def step_9_prepare_candidates(
    bm25_results,
):
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 9")
    logger.info("==========================================")

    candidate_map = {}

    for result in bm25_results:

        source_id = result.get(
            "source_id"
        )

        sku = result.get(
            "sku"
        )

        candidates = result.get(
            "candidates",
            [],
        )

        # ----------------------------------------------------
        # Convert BM25 results to scoring structure
        #
        # product_code -> (semantic_score, lexical_score)
        # ----------------------------------------------------

        source_candidates = {}

        for product_code, lexical_score in candidates:

            if not product_code:
                continue

            source_candidates[
                product_code
            ] = (
                0.0,
                float(lexical_score),
            )

        candidate_map[source_id] = {
            "sku": sku,
            "candidates": source_candidates,
        }

        logger.info(
            "Source ID=%s | SKU=%r | "
            "candidates=%d",
            source_id,
            sku,
            len(source_candidates),
        )

    logger.info("------------------------------------------")

    logger.info(
        "Source records with candidates: %d",
        len(candidate_map),
    )

    total_candidates = sum(
        len(item["candidates"])
        for item in candidate_map.values()
    )

    logger.info(
        "Total candidate records: %d",
        total_candidates,
    )

    logger.info("------------------------------------------")

    # --------------------------------------------------------
    # Display first few candidates
    # --------------------------------------------------------

    # for index, (
    #     source_id,
    #     result,
    # ) in enumerate(
    #     candidate_map.items(),
    #     start=1,
    # ):

    #     logger.info(
    #         "Source ID=%s | SKU=%r",
    #         source_id,
    #         result["sku"],
    #     )

    #     for rank, (
    #         product_code,
    #         scores,
    #     ) in enumerate(
    #         list(
    #             result["candidates"].items()
    #         )[:5],
    #         start=1,
    #     ):

    #         semantic_score, lexical_score = scores

    #         logger.info(
    #             "   %d. product=%s | "
    #             "semantic=%.4f | lexical=%.4f",
    #             rank,
    #             product_code,
    #             semantic_score,
    #             lexical_score,
    #         )

    #     if index >= 5:
    #         break

    # logger.info("------------------------------------------")

    # logger.info(
    #     "STEP 9 COMPLETED - CANDIDATES READY"
    # )

    return candidate_map


# ============================================================
# STEP 10 - VECTOR SEARCH
# ============================================================

def step_10_vector_search(
    conn,
    batch,
    top_k: int = 20,
):
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 10")
    logger.info("==========================================")

    if not batch:
        logger.info(
            "Source batch is empty"
        )
        return {}

    logger.info(
        "Source records in batch: %d",
        len(batch),
    )

    logger.info(
        "Vector Top-K: %d",
        top_k,
    )

    vector_results = {}

    # --------------------------------------------------------
    # Process each source SKU
    # --------------------------------------------------------

    for source in batch:

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

        # ----------------------------------------------------
        # Get source title
        # ----------------------------------------------------

        source_title = (
            getattr(
                source,
                "normalized_title",
                None,
            )
            or getattr(
                source,
                "title",
                None,
            )
            or ""
        ).strip()

        if not source_title:

            logger.warning(
                "Source ID=%s | SKU=%r | "
                "empty title - skipping vector search",
                source_id,
                sku,
            )

            vector_results[source_id] = {
                "sku": sku,
                "query": "",
                "candidates": [],
            }

            continue

        try:

            # ------------------------------------------------
            # Generate embedding
            # ------------------------------------------------

            query_vector = embedder.embed_one(
                source_title,
                tag="batch_flow",
            )

            logger.info(
                "Source ID=%s | SKU=%r | "
                "embedding generated",
                source_id,
                sku,
            )

            # ------------------------------------------------
            # Vector search
            # ------------------------------------------------

            candidates = stage_candidates.semantic_search(
                conn=conn,
                query_vector=query_vector,
                k=top_k,
            )

            # ------------------------------------------------
            # Store result
            # ------------------------------------------------

            vector_results[source_id] = {
                "sku": sku,
                "query": source_title,
                "candidates": candidates,
            }

            logger.info(
                "Source ID=%s | SKU=%r | "
                "vector candidates=%d",
                source_id,
                sku,
                len(candidates),
            )

            # ------------------------------------------------
            # Display Top 5
            # ------------------------------------------------

            # for rank, (
            #     product_code,
            #     score,
            # ) in enumerate(
            #     candidates[:5],
            #     start=1,
            # ):

            #     logger.info(
            #         "   %d. product=%s | "
            #         "vector_score=%.4f",
            #         rank,
            #         product_code,
            #         score,
            #     )

        except Exception as exc:

            logger.exception(
                "Vector search failed | "
                "source_id=%s | sku=%r",
                source_id,
                sku,
            )

            vector_results[source_id] = {
                "sku": sku,
                "query": source_title,
                "candidates": [],
                "error": str(exc),
            }

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    successful = sum(
        1
        for result in vector_results.values()
        if not result.get("error")
    )

    total_candidates = sum(
        len(result.get("candidates", []))
        for result in vector_results.values()
    )

    logger.info("------------------------------------------")

    logger.info(
        "Vector search processed: %d source records",
        len(vector_results),
    )

    logger.info(
        "Successful vector searches: %d",
        successful,
    )

    logger.info(
        "Total vector candidates: %d",
        total_candidates,
    )

    logger.info("------------------------------------------")

    logger.info(
        "STEP 10 COMPLETED - "
        "VECTOR CANDIDATES GENERATED"
    )

    return vector_results


# ============================================================
# STEP 11 - MERGE BM25 + VECTOR CANDIDATES
# ============================================================

def step_11_merge_candidates(
    bm25_results,
    vector_results,
    eligible_master,
):
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 11")
    logger.info("==========================================")

    eligible_codes = {
        master.product_code
        for master in eligible_master
        if master.product_code
    }

    logger.info(
        "Eligible master product codes: %d",
        len(eligible_codes),
    )

    merged_results = {}

    # --------------------------------------------------------
    # BM25 results is a LIST
    # --------------------------------------------------------

    for bm25_result in bm25_results:

        source_id = bm25_result.get(
            "source_id"
        )

        sku = bm25_result.get(
            "sku"
        )

        query = bm25_result.get(
            "query",
            "",
        )

        bm25_candidates = bm25_result.get(
            "candidates",
            [],
        )

        # Vector results is currently a DICT
        vector_result = vector_results.get(
            source_id,
            {},
        )

        vector_candidates = vector_result.get(
            "candidates",
            [],
        )

        candidates = {}

        # ----------------------------------------------------
        # BM25
        # ----------------------------------------------------

        for product_code, lexical_score in bm25_candidates:

            if product_code not in eligible_codes:
                continue

            candidates[product_code] = (
                0.0,
                float(lexical_score),
            )

        # ----------------------------------------------------
        # Vector
        # ----------------------------------------------------

        for product_code, semantic_score in vector_candidates:

            if product_code not in eligible_codes:
                continue

            if product_code in candidates:

                _, lexical_score = candidates[
                    product_code
                ]

                candidates[product_code] = (
                    float(semantic_score),
                    lexical_score,
                )

            else:

                candidates[product_code] = (
                    float(semantic_score),
                    0.0,
                )

        merged_results[source_id] = {
            "sku": sku,
            "query": query,
            "candidates": candidates,
        }

        logger.info(
            "Source ID=%s | SKU=%r | "
            "BM25=%d | Vector=%d | Merged=%d",
            source_id,
            sku,
            len(bm25_candidates),
            len(vector_candidates),
            len(candidates),
        )

    logger.info("------------------------------------------")

    logger.info(
        "Source records processed: %d",
        len(merged_results),
    )

    total_candidates = sum(
        len(result["candidates"])
        for result in merged_results.values()
    )

    logger.info(
        "Total merged candidates: %d",
        total_candidates,
    )

    logger.info("------------------------------------------")

    logger.info(
        "STEP 11 COMPLETED - "
        "BM25 + VECTOR CANDIDATES MERGED"
    )

    return merged_results

# ============================================================
# STEP 12 - SCORE CANDIDATES
# ============================================================

def step_12_score_candidates(
    batch,
    merged_candidates,
    master_by_code,
    source_table
):
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 12")
    logger.info("==========================================")

    ranked_results = {}

    # --------------------------------------------------------
    # Process every source SKU
    # --------------------------------------------------------

    for source in batch:

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

        # ----------------------------------------------------
        # Get merged candidates
        # ----------------------------------------------------

        result = merged_candidates.get(
            source_id,
            {},
        )

        candidates = result.get(
            "candidates",
            {},
        )

        if not candidates:

            logger.warning(
                "Source ID=%s | SKU=%r | "
                "no candidates to score",
                source_id,
                sku,
            )

            #ranked_results[source_id] = []
            ranked_results[source_id] = {
                "source": source_product,
                "ranked": [],
            }

            continue

        # ----------------------------------------------------
        # Build SourceProduct
        #
        # Use the same fields as existing stage_ingest flow.
        # ----------------------------------------------------

        title = (
            getattr(
                source,
                "title",
                None,
            )
            or ""
        )

        clean_title = (
            getattr(
                source,
                "clean_title",
                None,
            )
            or title.lower()
        )

        category = (
            getattr(
                source,
                "category",
                None,
            )
            or ""
        )

        subcategory = (
            getattr(
                source,
                "subcategory",
                None,
            )
            or ""
        )

        brand = (
            getattr(
                source,
                "brand",
                None,
            )
            or ""
        )

        # ----------------------------------------------------
        # Channel
        #
        # Since source_table is channel-specific:
        #
        # staging.zepto_products
        #          ↓
        #       zepto
        # ----------------------------------------------------

        channel_key = get_channel_from_source_table(
            source_table
        )

        logger.info(
            "Source table=%s | channel=%s",
            source_table,
            channel_key,
        )

        channel = getattr(
            source,
            "channel",
            None,
        )

        # Fallback if the DB row does not contain channel
        if not channel:
            channel = channel_key

        # ----------------------------------------------------
        # Pack
        # ----------------------------------------------------

        pack_value = getattr(
            source,
            "pack_size",
            None,
        )

        if pack_value is None:

            pack_value = getattr(
                source,
                "pack_value",
                None,
            )

        if pack_value is not None:

            try:
                pack_value = float(
                    pack_value
                )
            except (
                TypeError,
                ValueError,
            ):
                pack_value = None

        pack_unit = getattr(
            source,
            "uom",
            None,
        )

        if pack_unit is None:

            pack_unit = getattr(
                source,
                "pack_unit",
                None,
            )

        # ----------------------------------------------------
        # Benefit / Ingredient
        # ----------------------------------------------------

        benefit = getattr(
            source,
            "product_benefit",
            None,
        )

        if benefit is None:

            benefit = getattr(
                source,
                "benefit",
                None,
            )

        ingredient = getattr(
            source,
            "ingredient",
            None,
        )

        # ----------------------------------------------------
        # Domain / Match Type
        # ----------------------------------------------------

        domain = getattr(
            source,
            "domain",
            None,
        ) or "other"

        match_type = getattr(
            source,
            "match_type",
            None,
        ) or "Standard"

        # ----------------------------------------------------
        # Create SourceProduct
        # ----------------------------------------------------

        source_product = SourceProduct(
            sku=sku,

            source="1ds",

            channel=channel,

            brand=brand,

            title=title,

            clean_title=clean_title,

            category=category,

            subcategory=subcategory,

            pack_value=pack_value,

            pack_unit=pack_unit,

            benefit=benefit,

            ingredient=ingredient,

            domain=domain,

            match_type=match_type,

            channel_key=channel_key,

            source_row_id=source_id,
        )

        # ----------------------------------------------------
        # Existing scoring logic
        # ----------------------------------------------------

        ranked = stage_scoring.rank_candidates(
            source_product,
            candidates,
            master_by_code,
        )

        ranked_results[source_id] = {
            "source": source_product,
            "ranked": ranked,
        }

        # ----------------------------------------------------
        # Log results
        # ----------------------------------------------------

        logger.info(
            "Source ID=%s | SKU=%r | "
            "ranked candidates=%d",
            source_id,
            sku,
            len(ranked),
        )

        # ----------------------------------------------------
        # Top 5
        # ----------------------------------------------------

        # for rank, (
        #     master,
        #     scores,
        # ) in enumerate(
        #     ranked[:5],
        #     start=1,
        # ):

        #     logger.info(
        #         "   %d. product=%s | "
        #         "ensemble=%.4f | "
        #         "semantic=%.4f | "
        #         "lexical=%.4f | "
        #         "category=%.4f | "
        #         "type=%.4f | "
        #         "pack=%.4f | "
        #         "overlap=%.4f",
        #         rank,
        #         master.product_code,
        #         scores.ensemble,
        #         scores.semantic,
        #         scores.lexical,
        #         scores.category,
        #         scores.type_align,
        #         scores.pack,
        #         scores.overlap,
        #     )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    scored_count = sum(
        1
        for ranked in ranked_results.values()
        if ranked
    )

    total_ranked = sum(
        len(ranked)
        for ranked in ranked_results.values()
    )

    logger.info("------------------------------------------")

    logger.info(
        "Source records scored: %d",
        len(ranked_results),
    )

    logger.info(
        "Source records with candidates: %d",
        scored_count,
    )

    logger.info(
        "Total ranked candidates: %d",
        total_ranked,
    )

    logger.info("------------------------------------------")

    logger.info(
        "STEP 12 COMPLETED - "
        "CANDIDATES SCORED AND RANKED"
    )

    return ranked_results


# ============================================================
# STEP 13 - DISPOSITION / LLM JUDGE ROUTING
# ============================================================

def step_13_disposition(
    ranked_results,
):
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 13")
    logger.info("==========================================")

    results = {}

    judge_count = 0
    matched_count = 0
    medium_count = 0
    low_count = 0
    no_equivalent_count = 0
    no_candidate_count = 0

    # --------------------------------------------------------
    # Process every source SKU
    # --------------------------------------------------------

    for source_id, result in ranked_results.items():

        source = result["source"]
        ranked = result["ranked"]

        sku = source.sku

        # ----------------------------------------------------
        # No candidates
        # ----------------------------------------------------

        if not ranked:

            results[source_id] = {
                "source": source,
                "ranked": [],
                "needs_judge": False,
                "preliminary_tier": (
                    "No Himalaya Equivalent"
                ),
                "disposition": None,
            }

            no_candidate_count += 1

            logger.info(
                "Source ID=%s | SKU=%r | "
                "NO CANDIDATES",
                source_id,
                sku,
            )

            continue

        # ----------------------------------------------------
        # Top candidate
        # ----------------------------------------------------

        top_master, top_scores = ranked[0]

        top_score = float(
            top_scores.ensemble
        )

        # ----------------------------------------------------
        # Check LLM judge requirement
        # ----------------------------------------------------

        needs_judge = (
            stage_disposition.needs_judge(
                ranked
            )
        )

        # ----------------------------------------------------
        # Preliminary disposition
        # ----------------------------------------------------

        preliminary_result = (
            stage_disposition.disposition(
                source,
                ranked,
            )
        )

        preliminary_tier = (
            preliminary_result.confidence_tier
        )

        resolution_method = (
            preliminary_result.resolution_method
        )

        # ----------------------------------------------------
        # Counters
        # ----------------------------------------------------

        if preliminary_tier == "Matched":
            matched_count += 1

        elif preliminary_tier == "Medium":
            medium_count += 1

        elif preliminary_tier == "Low Confidence":
            low_count += 1

        else:
            no_equivalent_count += 1

        if needs_judge:
            judge_count += 1

        # ----------------------------------------------------
        # Top-1 vs Top-2 margin
        # ----------------------------------------------------

        margin = None

        if len(ranked) > 1:

            _, second_scores = ranked[1]

            margin = (
                top_scores.ensemble
                - second_scores.ensemble
            )

        # ----------------------------------------------------
        # Store result
        # ----------------------------------------------------

        results[source_id] = {
            "source": source,
            "ranked": ranked,
            "top_product_code": (
                top_master.product_code
            ),
            "top_score": top_score,
            "margin": margin,
            "preliminary_tier": preliminary_tier,
            "resolution_method": resolution_method,
            "needs_judge": needs_judge,
            "disposition": preliminary_result,
        }

        # ----------------------------------------------------
        # Logging
        # ----------------------------------------------------

        logger.info(
            "Source ID=%s | SKU=%r | "
            "Top=%s | Score=%.4f | "
            "Margin=%s | Tier=%s | Judge=%s",
            source_id,
            sku,
            top_master.product_code,
            top_score,
            (
                f"{margin:.4f}"
                if margin is not None
                else "N/A"
            ),
            preliminary_tier,
            needs_judge,
        )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    logger.info("------------------------------------------")

    logger.info(
        "Source records processed: %d",
        len(results),
    )

    logger.info(
        "Matched: %d",
        matched_count,
    )

    logger.info(
        "Medium: %d",
        medium_count,
    )

    logger.info(
        "Low Confidence: %d",
        low_count,
    )

    logger.info(
        "No Himalaya Equivalent: %d",
        no_equivalent_count,
    )

    logger.info(
        "No Candidates: %d",
        no_candidate_count,
    )

    logger.info(
        "LLM Judge Required: %d",
        judge_count,
    )

    logger.info("------------------------------------------")

    logger.info(
        "STEP 13 COMPLETED - "
        "DISPOSITION / JUDGE ROUTING READY"
    )

    return results

# ============================================================
# STEP 14 - LLM JUDGE
# ============================================================

def step_14_llm_judge(
    disposition_results,
):
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 14")
    logger.info("==========================================")

    final_results = {}

    judge_count = 0
    skipped_count = 0

    for source_id, result in disposition_results.items():

        source = result["source"]
        ranked = result["ranked"]

        sku = source.sku

        # ----------------------------------------------------
        # CASE 1 - NO CANDIDATES
        # ----------------------------------------------------

        if not ranked:

            final_results[source_id] = {
                **result,
                "llm_ran": False,
                "llm_pick": None,
                "llm_confidence": float("nan"),
                "llm_reason": None,
                "match_results": [
                    stage_disposition.disposition_all(
                        source,
                        ranked,
                        llm_pick=None,
                        llm_confidence=float("nan"),
                        llm_reason=None,
                    )[0]
                ],
                "audit_entry": None,
            }

            skipped_count += 1

            logger.info(
                "Source ID=%s | SKU=%r | "
                "NO CANDIDATES",
                source_id,
                sku,
            )

            continue

        # ----------------------------------------------------
        # CASE 2 - LLM NOT REQUIRED
        # ----------------------------------------------------

        if not result["needs_judge"]:

            # IMPORTANT:
            # Use disposition_all() even when LLM is skipped.
            #
            # This creates:
            #   rank 1 -> own scores
            #   rank 2 -> own scores
            #   rank 3 -> own scores
            #
            # llm_confidence remains NaN for all.
            #
            match_results = (
                stage_disposition.disposition_all(
                    source,
                    ranked,
                    llm_pick=None,
                    llm_confidence=float("nan"),
                    llm_reason=None,
                )
            )

            # Keep only TOP 3
            match_results = match_results[:3]

            final_results[source_id] = {
                **result,
                "llm_ran": False,
                "llm_pick": None,
                "llm_confidence": float("nan"),
                "llm_reason": None,
                "match_results": match_results,
                "audit_entry": None,
            }

            skipped_count += 1

            logger.info(
                "Source ID=%s | SKU=%r | "
                "LLM skipped | candidates=%d",
                source_id,
                sku,
                len(match_results),
            )

            # Debug scores
            for match in match_results:

                ensemble = (
                    match.scores.ensemble
                    if match.scores is not None
                    else None
                )

                # logger.info(
                #     "   FINAL | rank=%d | product=%s | "
                #     "ensemble=%s | llm_confidence=%s",
                #     match.rank,
                #     (
                #         match.candidate.product_code
                #         if match.candidate
                #         else None
                #     ),
                #     ensemble,
                #     match.llm_confidence,
                # )

            continue

        # ----------------------------------------------------
        # CASE 3 - LLM REQUIRED
        # ----------------------------------------------------

        judge_count += 1

        logger.info(
            "Source ID=%s | SKU=%r | "
            "LLM judge required",
            source_id,
            sku,
        )

        try:

            # ------------------------------------------------
            # Call existing MCDA LLM judge
            # ------------------------------------------------

            (
                llm_pick,
                llm_confidence,
                llm_reason,
                audit_entry,
            ) = mcda_judge.judge(
                source,
                ranked,
            )

            if audit_entry is not None:
                audit_entry["sku"] = sku

            logger.info(
                "Source ID=%s | SKU=%r | "
                "LLM pick=%s | confidence=%s",
                source_id,
                sku,
                llm_pick,
                llm_confidence,
            )

            # ------------------------------------------------
            # IMPORTANT
            #
            # Use disposition_all(), NOT disposition()
            #
            # This ensures:
            #   - LLM winner becomes rank 1
            #   - winner gets its own ScoreBreakdown
            #   - alternatives retain their own scores
            #   - rank 2/3 do NOT inherit LLM confidence
            # ------------------------------------------------

            match_results = (
                stage_disposition.disposition_all(
                    source,
                    ranked,
                    llm_pick=llm_pick,
                    llm_confidence=llm_confidence,
                    llm_reason=llm_reason,
                )
            )

            # ------------------------------------------------
            # Keep only TOP 3
            # ------------------------------------------------

            match_results = match_results[:3]

            # ------------------------------------------------
            # Log final candidates
            # ------------------------------------------------

            for match in match_results:

                ensemble = (
                    match.scores.ensemble
                    if match.scores is not None
                    else None
                )

                # logger.info(
                #     "   FINAL | rank=%d | product=%s | "
                #     "ensemble=%s | llm_confidence=%s | "
                #     "method=%s",
                #     match.rank,
                #     (
                #         match.candidate.product_code
                #         if match.candidate
                #         else None
                #     ),
                #     ensemble,
                #     match.llm_confidence,
                #     match.resolution_method,
                # )

            final_results[source_id] = {
                **result,
                "llm_ran": True,
                "llm_pick": llm_pick,
                "llm_confidence": llm_confidence,
                "llm_reason": llm_reason,
                "match_results": match_results,
                "audit_entry": audit_entry,
            }

        except Exception as exc:

            logger.exception(
                "LLM judge failed | "
                "Source ID=%s | SKU=%r",
                source_id,
                sku,
            )

            # ------------------------------------------------
            # LLM failure:
            # Continue with normal pipeline disposition.
            # ------------------------------------------------

            match_results = (
                stage_disposition.disposition_all(
                    source,
                    ranked,
                    llm_pick=None,
                    llm_confidence=float("nan"),
                    llm_reason=None,
                )
            )

            match_results = match_results[:3]

            final_results[source_id] = {
                **result,
                "llm_ran": False,
                "llm_pick": None,
                "llm_confidence": float("nan"),
                "llm_reason": None,
                "match_results": match_results,
                "audit_entry": None,
                "llm_error": str(exc),
            }

            # Log fallback candidates

            for match in match_results:

                ensemble = (
                    match.scores.ensemble
                    if match.scores is not None
                    else None
                )

                logger.info(
                    "   FALLBACK | rank=%d | product=%s | "
                    "ensemble=%s",
                    match.rank,
                    (
                        match.candidate.product_code
                        if match.candidate
                        else None
                    ),
                    ensemble,
                )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    logger.info("------------------------------------------")

    logger.info(
        "Total source records: %d",
        len(final_results),
    )

    logger.info(
        "LLM judge executed: %d",
        judge_count,
    )

    logger.info(
        "LLM judge skipped: %d",
        skipped_count,
    )

    logger.info("------------------------------------------")

    logger.info(
        "STEP 14 COMPLETED - "
        "LLM JUDGE COMPLETE"
    )

    return final_results

# ============================================================
# STEP 15 - PERSIST BATCH RESULTS
# ============================================================

def step_15_persist_results(
    conn,
    run_id,
    final_results,
    source_table,
):
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 15")
    logger.info("==========================================")

    results = []
    audit_entries = []

    # --------------------------------------------------------
    # Build MatchResult list
    # --------------------------------------------------------

    for source_id, result in final_results.items():

        source = result.get("source")

        if source is None:
            logger.warning(
                "Source ID=%s has no SourceProduct. Skipping.",
                source_id,
            )
            continue

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # Do NOT rebuild MatchResult from ranked here.
        #
        # Step 14 already created the correct MatchResult
        # objects using disposition_all().
        # ----------------------------------------------------

        match_results = result.get(
            "match_results",
            [],
        )

        if not match_results:

            logger.info(
                "Source ID=%s | SKU=%r | "
                "No MatchResult candidates",
                source_id,
                source.sku,
            )

        else:

            logger.info(
                "Source ID=%s | SKU=%r | "
                "MatchResult count=%d",
                source_id,
                source.sku,
                len(match_results),
            )

        # ----------------------------------------------------
        # Add TOP 3 MatchResult objects
        # ----------------------------------------------------

        for match_result in match_results[:3]:

            # ------------------------------------------------
            # Debug final score BEFORE DB persistence
            # ------------------------------------------------

            ensemble_score = (
                match_result.scores.ensemble
                if match_result.scores is not None
                else None
            )

            # logger.info(
            #     "PERSIST | SKU=%s | "
            #     "rank=%d | product=%s | "
            #     "ensemble_score=%s | "
            #     "llm_confidence=%s | "
            #     "resolution=%s",
            #     source.sku,
            #     match_result.rank,
            #     (
            #         match_result.candidate.product_code
            #         if match_result.candidate
            #         else None
            #     ),
            #     ensemble_score,
            #     match_result.llm_confidence,
            #     match_result.resolution_method,
            # )

            results.append(
                match_result
            )

        # ----------------------------------------------------
        # LLM audit
        # ----------------------------------------------------

        audit_entry = result.get(
            "audit_entry"
        )

        if audit_entry:

            audit_entries.append(
                audit_entry
            )

    # --------------------------------------------------------
    # Summary before persistence
    # --------------------------------------------------------

    logger.info("------------------------------------------")

    logger.info(
        "Mapping rows prepared: %d",
        len(results),
    )

    logger.info(
        "LLM audit rows prepared: %d",
        len(audit_entries),
    )

    # --------------------------------------------------------
    # Validate rank sequence
    # --------------------------------------------------------

    for source_id, result in final_results.items():

        source = result.get("source")

        if source is None:
            continue

        source_results = [
            r
            for r in results
            if r.source.source_row_id
            == source.source_row_id
        ]

        ranks = [
            r.rank
            for r in source_results
        ]

        if ranks:
            logger.info(
                "VALIDATE | SKU=%s | ranks=%s",
                source.sku,
                ranks,
            )

    logger.info("------------------------------------------")

    # --------------------------------------------------------
    # Resolve channel
    # --------------------------------------------------------

    channel_key = get_channel_from_source_table(
        source_table
    )

    logger.info(
        "Source table=%s | channel=%s",
        source_table,
        channel_key,
    )

    # --------------------------------------------------------
    # Persist mapping results
    # --------------------------------------------------------

    persisted_count = 0

    if results:

        logger.info(
            "Resolving channel tables for channel=%s",
            channel_key,
        )

        channel_tables = (
            db_models.resolve_channel_tables(
                conn.engine,
                channel_key,
            )
        )

        logger.info(
            "Writing %d mapping rows",
            len(results),
        )

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # db.py already correctly calculates:
        #
        # rank 1:
        #   max(LLM confidence, own ensemble)
        #
        # rank 2/3:
        #   own ensemble because llm_confidence = NaN
        #
        # ----------------------------------------------------

        db.write_staging_mapping(
            conn,
            channel_tables,
            run_id,
            results,
        )

        persisted_count = len(results)

        logger.info(
            "Mapping rows persisted: %d",
            persisted_count,
        )

    else:

        logger.info(
            "No mapping results to persist"
        )

    # --------------------------------------------------------
    # Persist LLM audit
    # --------------------------------------------------------

    if audit_entries:

        logger.info(
            "Writing %d LLM audit records",
            len(audit_entries),
        )

        db.write_llm_audit(
            conn,
            run_id,
            audit_entries,
        )

        logger.info(
            "LLM audit records persisted: %d",
            len(audit_entries),
        )

    else:

        logger.info(
            "No LLM audit records to persist"
        )

    # --------------------------------------------------------
    # Final summary
    # --------------------------------------------------------

    logger.info("------------------------------------------")

    logger.info(
        "STEP 15 SUMMARY"
    )

    logger.info(
        "Source records processed: %d",
        len(final_results),
    )

    logger.info(
        "Mapping rows persisted: %d",
        persisted_count,
    )

    logger.info(
        "LLM audit rows persisted: %d",
        len(audit_entries),
    )

    logger.info(
        "Channel: %s",
        channel_key,
    )

    logger.info("------------------------------------------")

    logger.info(
        "STEP 15 COMPLETED - "
        "BATCH RESULTS PERSISTED"
    )

    return {
        "source_count": len(final_results),
        "mapping_count": persisted_count,
        "audit_count": len(audit_entries),
        "channel": channel_key,
    }

# ============================================================
# STEP 16 - MARK SOURCE RECORDS COMPLETED
# ============================================================

def step_16_mark_completed(
    conn,
    source_table,
    batch,
    status
):
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 16")
    logger.info("==========================================")

    if not batch:
        logger.info(
            "No source records in batch"
        )
        return 0

    # --------------------------------------------------------
    # Resolve source table
    # --------------------------------------------------------

    if "." not in source_table:
        raise ValueError(
            "source-table must be in schema.table format"
        )

    source_schema, source_name = source_table.split(
        ".",
        1,
    )

    source = db_models.get_table(
        conn.engine,
        source_schema,
        source_name,
    )

    # --------------------------------------------------------
    # Get source IDs from current batch
    # --------------------------------------------------------

    source_ids = [
        getattr(row, "id", None)
        for row in batch
        if getattr(row, "id", None) is not None
    ]

    if not source_ids:
        logger.warning(
            "No source IDs found in current batch"
        )
        return 0

    logger.info(
        "Source records to complete: %d",
        len(source_ids),
    )

    # --------------------------------------------------------
    # Update only PENDING records
    # --------------------------------------------------------

    result = conn.execute(
        sa.update(source)
        .where(
            source.c.id.in_(source_ids)
        )
        .where(
            source.c.mapping_status == "PENDING"
        )
        .values(
            mapping_status=status
        )
    )

    updated_count = result.rowcount

    # --------------------------------------------------------
    # Commit status update
    # --------------------------------------------------------

    conn.commit()

    logger.info(
        "Source records marked COMPLETED: %d",
        updated_count,
    )

    # --------------------------------------------------------
    # Check if some records were not updated
    # --------------------------------------------------------

    if updated_count != len(source_ids):

        logger.warning(
            "Expected to update %d records, "
            "but updated %d records",
            len(source_ids),
            updated_count,
        )

    logger.info("------------------------------------------")

    logger.info(
        "STEP 16 COMPLETED - "
        "SOURCE STATUS UPDATED"
    )

    return updated_count