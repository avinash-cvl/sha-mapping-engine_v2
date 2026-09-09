"""Stage 1 -- deterministic resolution.

Two mechanisms: (1) an approved-crosswalk short-circuit -- a SKU a steward
(or the pipeline's own auto-approve) already resolved in a prior run skips
retrieval/scoring/LLM entirely; (2) hard identifier matching (GTIN/EAN/ASIN)
-- not yet implemented, no identifier columns exist in the current source
files.

The crosswalk is per-channel (app.{channel}_product_crosswalk /
_competitor_product_crosswalk), so the short-circuit check unions across
every active channel from config.channels -- there's no single "the
crosswalk table" anymore.
"""
from __future__ import annotations

import sqlalchemy as sa

from common import db
from common import db_models
from common.models import MasterProduct, MatchResult, ScoreBreakdown, SourceProduct


# SQL Server hard-caps bound parameters at 2100 per query. This check unions
# across every channel's crosswalk + crosswalk_competitor table (10 SELECTs
# at 5 active channels), so the SKU list is chunked to keep each union's
# total parameter count well under that -- 150 skus * 10 subqueries = 1500.
_SKU_CHUNK_SIZE = 150


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def load_approved_crosswalk(conn: db.Connection, skus: list[str]) -> dict[str, dict[str, object]]:
    """Return {sku: row} for SKUs already at match_rank=1 with an
    approved/auto_approved status in any active channel's crosswalk.

    Rows whose staging review_status now says Rejected are excluded even when
    the crosswalk still holds an approval. Measured on live data: 4 SKUs were
    approved by one reviewer and later REJECTED by another, and the rejection
    only updated staging.*_products -- the crosswalk row was never cleaned up.
    The short-circuit then kept treating them as settled, so they were skipped
    by every subsequent run and carried a stale Deterministic status with no
    mapping row at all. A later rejection is the newer decision and has to win.
    """
    if not skus:
        return {}
    engine = conn.engine
    channel_targets: list[tuple[sa.Table, sa.Table | None]] = []
    for channel_key in db_models.list_active_channels(engine):
        channel_tables = db_models.resolve_channel_tables(engine, channel_key)
        products = getattr(channel_tables, "products", None)
        channel_targets.append((channel_tables.crosswalk, products))
        channel_targets.append((channel_tables.crosswalk_competitor, products))
    if not channel_targets:
        return {}

    result: dict[str, dict[str, object]] = {}
    for sku_chunk in _chunks(skus, _SKU_CHUNK_SIZE):
        selects = [
            sa.select(target.c.sku, target.c.product_code, target.c.ensemble_score)
            .where(
                target.c.sku.in_(sku_chunk),
                target.c.match_rank == 1,
                # Case-insensitive: the portal writes 'Approved' while this
                # filter listed only the lower-case spellings, so a portal
                # approval was silently invisible to the short-circuit.
                sa.func.lower(sa.func.ltrim(sa.func.rtrim(target.c.match_status)))
                .in_(("auto_approved", "approved")),
            )
            for target, _ in channel_targets
        ]
        rows = conn.execute(sa.union_all(*selects)).all()
        for row in rows:
            result[str(row.sku)] = {"product_code": row.product_code, "ensemble_score": row.ensemble_score}

    # Drop anything a steward has since rejected. Done as a second pass rather
    # than a join because the crosswalk and the source table are per-channel
    # and a SKU only ever belongs to one of them.
    rejected: set[str] = set()
    seen_products: set[str] = set()
    for _, products in channel_targets:
        if products is None or "review_status" not in products.c:
            continue
        if products.name in seen_products:
            continue  # crosswalk and crosswalk_competitor share one products table
        seen_products.add(products.name)
        for sku_chunk in _chunks(list(result), _SKU_CHUNK_SIZE):
            if not sku_chunk:
                continue
            rows = conn.execute(
                sa.select(products.c.sku).where(
                    products.c.sku.in_(sku_chunk),
                    sa.func.lower(sa.func.ltrim(sa.func.rtrim(products.c.review_status))) == "rejected",
                )
            ).all()
            rejected.update(str(row.sku) for row in rows)

    return {sku: row for sku, row in result.items() if sku not in rejected}


def apply_crosswalk(
    source: SourceProduct,
    crosswalk_row: dict[str, object],
    master_by_code: dict[str, MasterProduct],
) -> MatchResult:
    """Build a synthetic rank-1 result for a crosswalk-approved SKU. No
    scoring, no LLM -- this row already has a governed answer."""
    product_code = str(crosswalk_row["product_code"])
    ensemble = crosswalk_row.get("ensemble_score")
    ensemble_score = float(str(ensemble)) if ensemble is not None else float("nan")
    return MatchResult(
        source=source,
        candidate=master_by_code.get(product_code),
        rank=1,
        scores=ScoreBreakdown(
            semantic=float("nan"), lexical=float("nan"), category=float("nan"),
            type_align=float("nan"), pack=float("nan"), overlap=float("nan"),
            ensemble=ensemble_score,
        ),
        confidence_tier="DeterministicMatch",
        resolution_method="crosswalk_deterministic",
        llm_confidence=float("nan"),
    )
