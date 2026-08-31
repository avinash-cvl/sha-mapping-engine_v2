from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import sqlalchemy as sa

from common import db
from common import db_models
from common.models import MasterProduct
from oneds_master.stages import stage_candidates
#from services.embedding_service import get_embedder
from common import embedder
from common.models import SourceProduct
from oneds_master.stages import stage_attributes
from oneds_master.stages import stage_deterministic
from oneds_master.stages import stage_lexicon
from oneds_master.stages import stage_scoring
from oneds_master.stages  import stage_disposition
from agents import attribute_fallback, mcda_judge, synonyms
from oneds_master.category.core import NO_EQ, UNCLS
from oneds_master.category.resolve import pack_targets, resolve
from oneds_master.category.rules import PACKS
from common.models import MatchResult
import sqlalchemy as sa
import common.config as C
from common import db_models

import math

logger = logging.getLogger(__name__)


def build_source_product(source, source_table: str) -> SourceProduct:
    """Build a SourceProduct from one raw staging row, up front -- before
    lexicon/attribute/candidate generation, same stage order as flow.py.
    Extracted from the old step_12 field-mapping so lexicon/attributes/
    synonyms (which all need a SourceProduct) can run ahead of scoring
    instead of after it."""
    sku = getattr(source, "sku", None)
    source_id = getattr(source, "id", None)

    title = getattr(source, "title", None) or ""
    clean_title = (getattr(source, "clean_title", None) or title.lower()).strip()
    category = getattr(source, "category", None) or ""
    subcategory = getattr(source, "subcategory", None) or ""
    brand = getattr(source, "brand", None) or ""

    channel_key = get_channel_from_source_table(source_table)
    channel = getattr(source, "channel", None) or channel_key

    pack_value = getattr(source, "pack_size", None)
    if pack_value is None:
        pack_value = getattr(source, "pack_value", None)
    if pack_value is not None:
        try:
            pack_value = float(pack_value)
        except (TypeError, ValueError):
            pack_value = None

    pack_unit = getattr(source, "uom", None)
    if pack_unit is None:
        pack_unit = getattr(source, "pack_unit", None)

    benefit = getattr(source, "product_benefit", None)
    if benefit is None:
        benefit = getattr(source, "benefit", None)

    ingredient = getattr(source, "ingredient", None)

    domain = getattr(source, "domain", None) or "other"
    match_type = getattr(source, "match_type", None) or "Standard"

    return SourceProduct(
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


def run_lexicon_attributes_and_synonyms(
    source_product: SourceProduct, use_llm: bool
) -> tuple[SourceProduct, list[str], list[dict]]:
    """Stages 2/3/4 from flow.py -- Lexicon expansion (currently a no-op
    passthrough, same as flow.py), rule-based pack parsing + LLM
    sub_brand/variant fallback, then synonym expansion. Mirrors flow.py's
    run_lexicon + run_attributes + the synonyms.expand() call inside
    run_candidates, just for one source at a time (batch_flow already
    parallelizes per-SKU via process_one_sku's thread pool, so no
    separate ThreadPoolExecutor is needed in here).

    No clean_title -> terms cache here (unlike flow.py's
    synonym_terms_by_title dedup): clean_title is effectively unique per
    SKU in this data, so a cache would only add bookkeeping with no
    LLM-call savings -- see the earlier discussion. Every SKU just calls
    synonyms.expand() directly.

    Returns (attributed_source, synonym_terms, audit_entries)."""
    expanded = stage_lexicon.expand(source_product)
    attributed = stage_attributes.extract_attributes(expanded)

    audit_entries: list[dict] = []

    if use_llm and attributed.sub_brand is None and attributed.variant is None:
        sub_brand, variant, audit_entry = attribute_fallback.extract(
            attributed.title, attributed.brand
        )
        audit_entry["sku"] = attributed.sku
        audit_entries.append(audit_entry)
        attributed = replace(attributed, sub_brand=sub_brand, variant=variant)

    synonym_terms: list[str] = []
    if use_llm:
        synonym_terms, synonym_audit_entry = synonyms.expand(attributed.clean_title)
        synonym_audit_entry["sku"] = attributed.sku
        audit_entries.append(synonym_audit_entry)

    return attributed, synonym_terms, audit_entries


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
            source.c.brand.in_(C.HIMALAYA_BRANDS),
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
            source.c.brand.in_(C.HIMALAYA_BRANDS),
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
            master.c.is_active == 1
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


# NOTE: master-row embedding backfill (rows_needing_embedding +
# write_master_embeddings) is intentionally NOT a step in this file. It
# runs as its own separate job upstream of batch_flow.py, so every master
# row is assumed to already have an embedding by the time step 10's
# vector search runs.


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

        # ----------------------------------------------------
        # Widen the pool with every target the V2 category pack
        # for this 1DS category can emit.
        #
        # The BM25 index (step 7) and the vector search (step 10)
        # both operate over eligible_master, so a target the V2
        # resolver can pick but config.oneds_master_category_mapping
        # does not list would never be retrievable -- and the
        # row-level gate in process_one_sku would then find nothing
        # and fall back to ungated.
        #
        # Keyed into the same dict by product_code, so this is
        # idempotent and can only ever WIDEN the pool, never
        # narrow it. Recall cannot go down here.
        # ----------------------------------------------------

        pack = PACKS.get(oneds_category)

        if pack is not None:

            before_widening = len(eligible_by_code)

            for cat, sub in pack_targets(pack):

                for master in master_lookup.get(
                    (
                        cat.strip().lower(),
                        sub.strip().lower(),
                    ),
                    [],
                ):

                    eligible_by_code[
                        master.product_code
                    ] = master

            logger.info(
                "V2 pack widening: %r | %d -> %d (+%d)",
                oneds_category,
                before_widening,
                len(eligible_by_code),
                len(eligible_by_code) - before_widening,
            )

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
            source.c.brand.in_(C.HIMALAYA_BRANDS),
            #source.c.sku == "bbf07424-effc-46d2-9dbd-d6761a0edbf4"
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
# STEP 6B - DETERMINISTIC CROSSWALK SHORT-CIRCUIT
# ============================================================
# flow.py's run_deterministic checks the approved crosswalk before doing
# any retrieval/scoring/LLM work; batch_flow.py had no equivalent, so
# every SKU -- even ones a steward already approved in a prior run --
# went through the full BM25/vector/scoring/judge pipeline again.

def step_6b_apply_crosswalk_shortcircuit(
    conn: db.Connection,
    source_table: str,
    master_by_code: dict[str, MasterProduct],
    batch,
):
    """Splits `batch` into (resolved, remaining):
      - resolved: MatchResult objects for SKUs already approved/
        auto_approved at match_rank=1 in any active channel's crosswalk
        (stage_deterministic.apply_crosswalk) -- these skip retrieval,
        scoring, and the LLM judge entirely.
      - remaining: raw rows for SKUs still needing the full pipeline.
    Caller is responsible for persisting `resolved` and marking those
    source rows completed (see run_persist_sku in batch_flow.py)."""
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 6B")
    logger.info("==========================================")

    if not batch:
        return [], []

    skus = [getattr(row, "sku", None) for row in batch]
    skus = [s for s in skus if s]

    approved = stage_deterministic.load_approved_crosswalk(conn, skus)

    if not approved:
        logger.info(
            "Crosswalk short-circuit: 0 / %d SKUs already approved",
            len(batch),
        )
        return [], list(batch)

    resolved: list[MatchResult] = []
    remaining = []

    for row in batch:
        sku = getattr(row, "sku", None)
        crosswalk_row = approved.get(sku) if sku else None
        if crosswalk_row is None:
            remaining.append(row)
            continue

        source_product = build_source_product(row, source_table)
        result = stage_deterministic.apply_crosswalk(
            source_product, crosswalk_row, master_by_code
        )
        resolved.append(result)

    logger.info(
        "Crosswalk short-circuit: %d / %d SKUs resolved | %d remaining",
        len(resolved),
        len(batch),
        len(remaining),
    )

    logger.info("STEP 6B COMPLETED")

    return resolved, remaining


# ============================================================
# STEP 6C - CATEGORY TERMINAL SHORT-CIRCUIT
# ============================================================
# Rows the V2 resolver confidently classifies as terminal should not be
# embedded, retrieved, scored, or sent to the LLM judge. Mirrors step 6B:
# it runs inside the per-group loop, before step 7B, and hands back
# (resolved, remaining).

# Terminal -> (confidence_tier, resolution_method). UNRESOLVED is
# deliberately absent: it means "the rules could not decide", not "there is
# nothing here", so those rows MUST keep flowing through the normal
# pipeline. Short-circuiting them would silently lose recall.
_CATEGORY_TERMINAL_DISPOSITION = {
    NO_EQ: ("No Himalaya Equivalent", "category_no_equivalent"),
    UNCLS: ("No Himalaya Equivalent", "category_unclassified"),
}

# The resolution_methods above, for determine_mapping_status().
_CATEGORY_TERMINAL_METHODS = {
    method for _tier, method in _CATEGORY_TERMINAL_DISPOSITION.values()
}


def step_6c_apply_category_shortcircuit(
    source_table: str,
    batch,
    source_products: dict | None = None,
):
    """Splits `batch` into (resolved, remaining):
      - resolved: {source_id: MatchResult} for rows the V2 category
        resolver closed as NO HGML EQUIVALENT or UNCLASSIFIED -- these skip
        retrieval, scoring, and the LLM judge entirely. candidate/scores are
        None, and the resolver's evidence rides along on the MatchResult.
      - remaining: raw rows for SKUs still needing the full pipeline,
        including every UNRESOLVED row.

    Caller marks `resolved` completed via step_16_mark_completed rather
    than writing mapping rows: these rows have no candidate, and
    staging.*_product_mapping declares product_code NOT NULL. That matches
    what every other no-candidate path here already does (empty eligible
    group, crosswalk short-circuit)."""
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 6C")
    logger.info("==========================================")

    if not batch:
        return {}, []

    resolved: dict = {}
    remaining = []

    for row in batch:

        source_id = getattr(row, "id", None)

        source_product = (source_products or {}).get(source_id)
        if source_product is None:
            source_product = build_source_product(row, source_table)

        decision = resolve(source_product)

        disposition = (
            _CATEGORY_TERMINAL_DISPOSITION.get(decision.terminal)
            if decision.terminal
            else None
        )

        if disposition is None:
            remaining.append(row)
            continue

        confidence_tier, resolution_method = disposition

        resolved[source_id] = MatchResult(
            source=source_product,
            candidate=None,
            rank=1,
            scores=None,
            confidence_tier=confidence_tier,
            resolution_method=resolution_method,
            llm_confidence=float("nan"),
            category_evidence=decision.evidence,
            category_confidence=decision.confidence,
        )

        logger.debug(
            "Category short-circuit | SKU=%r | %s | evidence=%r",
            getattr(row, "sku", None),
            decision.terminal,
            decision.evidence,
        )

    logger.info(
        "Category short-circuit: %d / %d rows closed | %d remaining",
        len(resolved),
        len(batch),
        len(remaining),
    )

    logger.info("STEP 6C COMPLETED")

    return resolved, remaining


# ============================================================
# STEP 7B - LEXICON EXPANSION + ATTRIBUTE EXTRACTION
# ============================================================
# flow.py's run_lexicon (Lexicon expansion, currently a no-op passthrough)
# and run_attributes (rule-based pack parsing + LLM sub_brand/variant
# fallback) both ran before candidate generation; batch_flow.py called
# neither, so pack_value/pack_unit only ever came from raw DB columns and
# sub_brand/variant were never populated.

def step_7b_prepare_source_products(
    batch,
    source_table: str,
    use_llm: bool,
    max_workers: int = 10,
):
    """Builds a SourceProduct per row (build_source_product), then applies
    Lexicon expansion + attribute extraction + synonym expansion
    (run_lexicon_attributes_and_synonyms) to each. The LLM calls
    (attribute_fallback, synonyms.expand) are independent per-row
    requests, so they're fanned out across one thread pool the same way
    flow.py's run_attributes does -- previously this was two separate
    steps (7B for lexicon/attributes, 7C for a batch-level synonym cache)
    with two thread pools; merged into one pass since clean_title is
    effectively unique per SKU here, so the cache in the old step 7C
    never actually deduped anything.

    Returns (source_products: {source_id: SourceProduct},
             synonym_terms: {source_id: [terms]},
             audit_entries: [dict])."""
    logger.info("==========================================")
    logger.info("BATCH FLOW - STEP 7B")
    logger.info("==========================================")

    if not batch:
        return {}, {}, []

    built = {
        getattr(row, "id", None): build_source_product(row, source_table)
        for row in batch
    }

    if not use_llm:
        source_products = {
            sid: stage_attributes.extract_attributes(stage_lexicon.expand(sp))
            for sid, sp in built.items()
        }
        logger.info(
            "STEP 7B COMPLETED - %d products (LLM calls skipped)",
            len(source_products),
        )
        return source_products, {}, []

    source_products: dict = {}
    synonym_terms: dict = {}
    audit_entries: list[dict] = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        ids = list(built.keys())
        futures = {
            executor.submit(
                run_lexicon_attributes_and_synonyms, built[sid], use_llm
            ): sid
            for sid in ids
        }
        for future in futures:
            sid = futures[future]
            attributed, terms, entries = future.result()
            source_products[sid] = attributed
            synonym_terms[sid] = terms
            audit_entries.extend(entries)

    logger.info(
        "STEP 7B COMPLETED - %d products | %d LLM calls",
        len(source_products),
        len(audit_entries),
    )

    return source_products, synonym_terms, audit_entries


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
    source_products: dict | None = None,
    synonym_terms: dict | None = None,
):
    logger.debug("==========================================")
    logger.debug("BATCH FLOW - STEP 8")
    logger.debug("==========================================")

    if not bm25_index:
        logger.warning(
            "BM25 index is empty. "
            "Skipping BM25 search."
        )
        return []

    if not batch:
        logger.debug(
            "Source batch is empty."
        )
        return []

    logger.debug(
        "Source records in batch: %d",
        len(batch),
    )

    logger.debug(
        "BM25 Top-N: %d",
        top_n,
    )

    logger.debug(
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
        # BM25 SEARCH -- across all 4 representations
        # (title / title+ingredient / title+benefit / all),
        # aggregated per product_code by max lexical score. Uses
        # the already lexicon-expanded / attribute-extracted
        # SourceProduct if one was built upstream (process_one_sku
        # does this); falls back to a bare representation off the
        # raw row otherwise, so this still works standalone.
        # ----------------------------------------------------

        source_id = getattr(source, "id", None)
        source_product = (source_products or {}).get(source_id)
        if source_product is not None:
            reps = stage_candidates.get_source_representations(source_product)
            terms_for_source = (synonym_terms or {}).get(source_id, [])
        else:
            reps = {"title": source_title.lower()}
            terms_for_source = []
        syn_suffix = f" {' '.join(terms_for_source)}" if terms_for_source else ""

        lex_max: dict[str, float] = {}
        for rep_key, rep_text in reps.items():
            if not rep_text:
                continue
            query_text = f"{rep_text}{syn_suffix}"
            rep_candidates = stage_candidates.lexical_search(
                bm25_index,
                eligible_master,
                query_text,
                k=top_n,
            )
            for product_code, score in rep_candidates:
                lex_max[product_code] = max(
                    lex_max.get(product_code, 0.0), float(score)
                )

        candidates = sorted(
            lex_max.items(), key=lambda kv: kv[1], reverse=True
        )[:top_n]

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

        logger.debug(
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

        #     logger.debug(
        #         "   %d. %s",
        #         rank,
        #         candidate,
        #     )

    # logger.debug("------------------------------------------")

    logger.debug(
        "BM25 search completed for %d source records",
        len(all_results),
    )

    # logger.debug(
    #     "STEP 8 COMPLETED - TOP-N CANDIDATES GENERATED"
    # )

    return all_results

# ============================================================
# STEP 9 - PREPARE CANDIDATES
# ============================================================

def step_9_prepare_candidates(
    bm25_results,
):
    logger.debug("==========================================")
    logger.debug("BATCH FLOW - STEP 9")
    logger.debug("==========================================")

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

        logger.debug(
            "Source ID=%s | SKU=%r | "
            "candidates=%d",
            source_id,
            sku,
            len(source_candidates),
        )

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("------------------------------------------")

        logger.debug(
            "Source records with candidates: %d",
            len(candidate_map),
        )

        total_candidates = sum(
            len(item["candidates"])
            for item in candidate_map.values()
        )

        logger.debug(
            "Total candidate records: %d",
            total_candidates,
        )

        logger.debug("------------------------------------------")

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

    #     logger.debug(
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

    #         logger.debug(
    #             "   %d. product=%s | "
    #             "semantic=%.4f | lexical=%.4f",
    #             rank,
    #             product_code,
    #             semantic_score,
    #             lexical_score,
    #         )

    #     if index >= 5:
    #         break

    # logger.debug("------------------------------------------")

    # logger.debug(
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
    source_products: dict | None = None,
):
    logger.debug("==========================================")
    logger.debug("BATCH FLOW - STEP 10")
    logger.debug("==========================================")

    if not batch:
        logger.debug(
            "Source batch is empty"
        )
        return {}

    logger.debug(
        "Source records in batch: %d",
        len(batch),
    )

    logger.debug(
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
            # Generate embeddings -- across all 4 representations
            # (title / title+ingredient / title+benefit / all).
            # embed_many() hits embedder's on-disk cache per text,
            # so reps this source shares with an earlier source
            # (e.g. identical title) are free.
            # ------------------------------------------------

            source_product = (source_products or {}).get(source_id)
            if source_product is not None:
                reps = stage_candidates.get_source_representations(source_product)
            else:
                reps = {"title": source_title.lower()}
            rep_keys = [k for k, v in reps.items() if v]
            rep_texts = [reps[k] for k in rep_keys]

            rep_vectors = embedder.embed_many(
                rep_texts,
                tag="batch_flow",
            )

            logger.debug(
                "Source ID=%s | SKU=%r | "
                "embeddings generated | reps=%d",
                source_id,
                sku,
                len(rep_texts),
            )

            # ------------------------------------------------
            # Vector search per representation, aggregated by
            # max semantic score per product_code.
            # ------------------------------------------------

            sem_max: dict[str, float] = {}
            for vector in rep_vectors:
                rep_candidates = stage_candidates.semantic_search(
                    conn=conn,
                    query_vector=vector,
                    k=top_k,
                )
                for product_code, score in rep_candidates:
                    sem_max[product_code] = max(
                        sem_max.get(product_code, 0.0), float(score)
                    )

            candidates = sorted(
                sem_max.items(), key=lambda kv: kv[1], reverse=True
            )[:top_k]

            # ------------------------------------------------
            # Store result
            # ------------------------------------------------

            vector_results[source_id] = {
                "sku": sku,
                "query": source_title,
                "candidates": candidates,
            }

            logger.debug(
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

            #     logger.debug(
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

    if logger.isEnabledFor(logging.DEBUG):
        successful = sum(
            1
            for result in vector_results.values()
            if not result.get("error")
        )

        total_candidates = sum(
            len(result.get("candidates", []))
            for result in vector_results.values()
        )

        logger.debug("------------------------------------------")

        logger.debug(
            "Vector search processed: %d source records",
            len(vector_results),
        )

        logger.debug(
            "Successful vector searches: %d",
            successful,
        )

        logger.debug(
            "Total vector candidates: %d",
            total_candidates,
        )

        logger.debug("------------------------------------------")

    logger.debug(
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
    logger.debug("==========================================")
    logger.debug("BATCH FLOW - STEP 11")
    logger.debug("==========================================")

    eligible_codes = {
        master.product_code
        for master in eligible_master
        if master.product_code
    }

    logger.debug(
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

        logger.debug(
            "Source ID=%s | SKU=%r | "
            "BM25=%d | Vector=%d | Merged=%d",
            source_id,
            sku,
            len(bm25_candidates),
            len(vector_candidates),
            len(candidates),
        )

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("------------------------------------------")

        logger.debug(
            "Source records processed: %d",
            len(merged_results),
        )

        total_candidates = sum(
            len(result["candidates"])
            for result in merged_results.values()
        )

        logger.debug(
            "Total merged candidates: %d",
            total_candidates,
        )

        logger.debug("------------------------------------------")

    logger.debug(
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
    source_table,
    source_products: dict | None = None,
):
    """Reuses the SourceProduct built earlier (build_source_product() +
    run_lexicon_and_attributes(), see process_one_sku) instead of
    rebuilding one from the raw row a second time -- that rebuild used to
    live here and threw away lexicon expansion / pack parsing / sub_brand
    /variant, since it was reconstructed straight from the untouched DB
    row. Falls back to a fresh build_source_product() call if the caller
    didn't pass one in, so this still works standalone."""
    logger.debug("==========================================")
    logger.debug("BATCH FLOW - STEP 12")
    logger.debug("==========================================")

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

        source_product = (source_products or {}).get(source_id)
        if source_product is None:
            source_product = build_source_product(source, source_table)

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

            ranked_results[source_id] = {
                "source": source_product,
                "ranked": [],
            }

            continue

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

        logger.debug(
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

        #     logger.debug(
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

    if logger.isEnabledFor(logging.DEBUG):
        scored_count = sum(
            1
            for ranked in ranked_results.values()
            if ranked
        )

        total_ranked = sum(
            len(ranked)
            for ranked in ranked_results.values()
        )

        logger.debug("------------------------------------------")

        logger.debug(
            "Source records scored: %d",
            len(ranked_results),
        )

        logger.debug(
            "Source records with candidates: %d",
            scored_count,
        )

        logger.debug(
            "Total ranked candidates: %d",
            total_ranked,
        )

        logger.debug("------------------------------------------")

    logger.debug(
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
    logger.debug("==========================================")
    logger.debug("BATCH FLOW - STEP 13")
    logger.debug("==========================================")

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

            logger.debug(
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

        logger.debug(
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

    logger.debug("------------------------------------------")

    logger.debug(
        "Source records processed: %d",
        len(results),
    )

    logger.debug(
        "Matched: %d",
        matched_count,
    )

    logger.debug(
        "Medium: %d",
        medium_count,
    )

    logger.debug(
        "Low Confidence: %d",
        low_count,
    )

    logger.debug(
        "No Himalaya Equivalent: %d",
        no_equivalent_count,
    )

    logger.debug(
        "No Candidates: %d",
        no_candidate_count,
    )

    logger.debug(
        "LLM Judge Required: %d",
        judge_count,
    )

    logger.debug("------------------------------------------")

    logger.debug(
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
    logger.debug("==========================================")
    logger.debug("BATCH FLOW - STEP 14")
    logger.debug("==========================================")

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

            logger.debug(
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

            logger.debug(
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

                # logger.debug(
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

        logger.debug(
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

            logger.debug(
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

                # logger.debug(
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

                logger.debug(
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

    logger.debug("------------------------------------------")

    logger.debug(
        "Total source records: %d",
        len(final_results),
    )

    logger.debug(
        "LLM judge executed: %d",
        judge_count,
    )

    logger.debug(
        "LLM judge skipped: %d",
        skipped_count,
    )

    logger.debug("------------------------------------------")

    logger.debug(
        "STEP 14 COMPLETED - "
        "LLM JUDGE COMPLETE"
    )

    return final_results

# ============================================================
# STEP 15 - ADD MAPPING DATA
# ============================================================

def determine_mapping_status(match_results):
    """Determine mapping status using Rank 1 only.

    Score used: max(final_score, ensemble_score).
    Thresholds: >= 0.86 -> AutoMatch, 0.61-0.85 -> StewardReview,
    < 0.61 -> LowConfidence.

    Moved here from batch_flow.py (was duplicated there, hand-copied from
    flow.py's _mapping_status/_none_if_nan) so step_15 can call it
    directly without importing back into batch_flow.py -- one
    implementation instead of two that could drift."""
    if not match_results:
        return "NoHimalayaEquivalent"

    rank_one = match_results[0]

    # A category-level finding is not a product-level verdict, so it routes to
    # a steward rather than closing the row on score alone. Without this
    # branch a scoreless short-circuit result would fall through to
    # "NoHimalayaEquivalent" below, which reads as a pipeline conclusion.
    # Purely additive: no pre-existing resolution_method is in this set, so
    # every other row takes exactly the path it did before.
    if getattr(rank_one, "resolution_method", None) in _CATEGORY_TERMINAL_METHODS:
        return "StewardReview"

    final_score = getattr(rank_one, "llm_confidence", None)
    if final_score is not None and final_score != final_score:  # NaN
        final_score = None

    ensemble_score = None
    if getattr(rank_one, "scores", None) is not None:
        ensemble_score = getattr(rank_one.scores, "ensemble", None)
        if ensemble_score is not None and ensemble_score != ensemble_score:  # NaN
            ensemble_score = None

    if final_score is None:
        final_score = ensemble_score
    elif ensemble_score is not None and ensemble_score > final_score:
        final_score = ensemble_score

    if final_score is None:
        return "NoHimalayaEquivalent"
    if final_score >= 0.86:
        return "AutoMatch"
    if final_score >= 0.61:
        return "StewardReview"
    return "LowConfidence"


def step_15_add_mapping_data(
    conn,
    run_id,
    final_results,
    source_table,
):
    """Adds this SKU's mapping data: writes rank 1/2/3 candidate rows
    (product_code, scores, confidence_tier) into
    staging.{channel}_product_mapping, flips the source product's
    mapping_status from PENDING to AutoMatch/StewardReview/LowConfidence/
    NoHimalayaEquivalent, and writes any LLM audit rows -- all in one
    transaction per SKU via db.persist_sku_disposition(). (Renamed from
    step_15_persist_results -- 'persist' was a generic name; this step's
    actual job is adding mapping data, not persistence in general.)"""
    logger.debug("==========================================")
    logger.debug("BATCH FLOW - STEP 15")
    logger.debug("==========================================")

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

            logger.debug(
                "Source ID=%s | SKU=%r | "
                "No MatchResult candidates",
                source_id,
                source.sku,
            )

        else:

            logger.debug(
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

            # logger.debug(
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
        # LLM audit -- judge's own entry (singular) plus any
        # pre-candidate-generation entries (attribute_fallback,
        # synonyms.expand) collected upstream and attached under
        # 'pre_audit_entries' (plural, list).
        # ----------------------------------------------------

        audit_entry = result.get(
            "audit_entry"
        )

        if audit_entry:

            audit_entries.append(
                audit_entry
            )

        for pre_entry in result.get("pre_audit_entries", []) or []:
            if pre_entry:
                audit_entries.append(pre_entry)

    # --------------------------------------------------------
    # Summary before persistence
    # --------------------------------------------------------

    logger.debug("------------------------------------------")

    logger.debug(
        "Mapping rows prepared: %d",
        len(results),
    )

    logger.debug(
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
            logger.debug(
                "VALIDATE | SKU=%s | ranks=%s",
                source.sku,
                ranks,
            )

    logger.debug("------------------------------------------")

    # --------------------------------------------------------
    # Resolve channel
    # --------------------------------------------------------

    channel_key = get_channel_from_source_table(
        source_table
    )

    logger.debug(
        "Source table=%s | channel=%s",
        source_table,
        channel_key,
    )

    # --------------------------------------------------------
    # Persist mapping results -- atomically, per SKU
    # --------------------------------------------------------
    #
    # Switched from db.write_staging_mapping() (bulk insert; its
    # delete-existing-rows guard is commented out in db.py, so a
    # replayed/rerun batch would double this table) to
    # db.persist_sku_disposition() -- one transaction per SKU that
    # deletes+inserts that SKU's mapping rows, updates its product
    # status, and deletes+inserts its audit rows, all commit-or-rollback
    # together. Same path flow.py's run_persist uses. This also folds
    # step_16's status update into the same transaction for this path,
    # instead of a second, non-atomic UPDATE afterward.

    persisted_count = 0
    skus_updated = 0
    channel_tables = None

    audit_by_sku: dict[str, list] = {}
    for entry in audit_entries:
        sku = entry.get("sku")
        if sku:
            audit_by_sku.setdefault(sku, []).append(entry)

    if results:

        logger.debug(
            "Resolving channel tables for channel=%s",
            channel_key,
        )

        channel_tables = db_models.resolve_channel_tables(
            conn.engine,
            channel_key,
        )

        for source_id, result in final_results.items():

            source = result.get("source")
            if source is None:
                continue

            sku_results = [
                r for r in results
                if r.source.source_row_id == source.source_row_id
            ]
            if not sku_results:
                continue

            mapping_status = determine_mapping_status(sku_results)

            db.persist_sku_disposition(
                conn,
                channel_tables,
                run_id,
                sku_results,
                mapping_status,
                audit_by_sku.pop(source.sku, []),
            )

            persisted_count += len(sku_results)
            skus_updated += 1

            logger.debug(
                "PERSISTED | SKU=%s | rows=%d | mapping_status=%s",
                source.sku,
                len(sku_results),
                mapping_status,
            )

        logger.debug(
            "Mapping rows persisted (atomic, per SKU): %d across %d SKUs",
            persisted_count,
            skus_updated,
        )

    else:

        logger.debug(
            "No mapping results to persist"
        )

    # --------------------------------------------------------
    # Any audit entries that didn't line up with a persisted SKU
    # (shouldn't normally happen -- persist_sku_disposition already
    # wrote each SKU's own audit rows above) still get written so
    # nothing is silently dropped.
    # --------------------------------------------------------

    orphaned_audit = [e for entries in audit_by_sku.values() for e in entries]
    if orphaned_audit:

        logger.debug(
            "Writing %d orphaned LLM audit records",
            len(orphaned_audit),
        )

        db.write_llm_audit(
            conn,
            run_id,
            orphaned_audit,
        )

    else:

        logger.debug(
            "No orphaned LLM audit records to persist"
        )

    # --------------------------------------------------------
    # Final summary
    # --------------------------------------------------------

    logger.debug("------------------------------------------")

    logger.debug(
        "STEP 15 SUMMARY"
    )

    logger.debug(
        "Source records processed: %d",
        len(final_results),
    )

    logger.debug(
        "Mapping rows persisted: %d",
        persisted_count,
    )

    logger.debug(
        "LLM audit rows persisted: %d",
        len(audit_entries),
    )

    logger.debug(
        "Channel: %s",
        channel_key,
    )

    logger.debug("------------------------------------------")

    logger.debug(
        "STEP 15 COMPLETED - "
        "MAPPING DATA ADDED"
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

    # getattr(row, "id", ...) handles raw DB rows (from step_6); the
    # source_row_id fallback handles SourceProduct objects (e.g. crosswalk-
    # resolved MatchResult.source in main()'s crosswalk short-circuit path).
    source_ids = [
        getattr(row, "id", None) or getattr(row, "source_row_id", None)
        for row in batch
        if (getattr(row, "id", None) or getattr(row, "source_row_id", None)) is not None
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