"""Mapping results as a spreadsheet, in the shape the team already reads.

WHY THIS SQL AND NOT A NEW ONE

The column list here is the query the team already shares with each other,
reproduced rather than reinvented. An export that returns "the same data,
slightly differently arranged" makes the recipient reconcile two layouts, so
the point of this module is that the file looks exactly like what they are
used to opening.

It spans two views -- staging.vw_channel_products and
vw_channel_all_products_mapping -- which union the four per-channel tables, so
one export can cover every channel without the caller naming them.

WHAT THE THREE SCOPES MEAN

  * run       -- the SKUs one execution touched. audit.engine_run records the
                 scope, not the SKU list, so this re-derives it from the run's
                 channel/engine/category. A run over an explicit SKU list is
                 therefore approximated by its scope, and the file says so.
  * unreviewed - the original query's WHERE: rows nobody has approved or
                 rejected. This is the review backlog.
  * rejected  -- rows a steward turned down, for handing to whoever fixes them.

Every scope keeps match_rank, so all three candidate rows per SKU come out --
the reviewer needs to see what was passed over, not only what won.
"""
from __future__ import annotations

import csv
import io

import sqlalchemy as sa

# The SELECT list, verbatim from what the team shares. Written out rather than
# built from a column list so it can be read against the original at a glance.
_SELECT = """
    p.channel,
    p.id,
    p.sku,
    p.title,
    p.ingredient,
    p.product_benefit,

    m.product_code,
    m.product_name,
    sp.normalized_product_group AS product_group,

    p.category    AS master_category,
    p.subcategory AS master_subcategory,
    p.pack_size   AS master_pack_size,
    p.uom         AS master_uom,

    sp.normalized_category    AS oneds_category,
    sp.normalized_subcategory AS oneds_subcategory,
    sp.normalized_pack_size   AS oneds_pack_size,
    sp.normalized_uom         AS oneds_uom,

    m.final_score,
    m.llm_reasoning,
    m.match_rank,
    p.brand,
    p.mapping_status,
    m.mapping_id,
    m.relationship_type,
    m.ensemble_score,
    m.confidence_level,
    m.lexical_score,
    m.vector_score,
    m.approved_at,

    -- Last, at the team's request: the verdict is what you scroll to after
    -- reading the match, not what you meet before it.
    p.review_status
"""

_FROM = """
FROM [staging].[vw_channel_products] AS p
INNER JOIN [staging].[vw_channel_all_products_mapping] AS m
    ON p.id = m.source_product_id
   AND p.channel = m.channel
LEFT JOIN [staging].[himalaya_products] AS sp
    ON sp.product_code = m.product_code
"""

# Column order for the file, matching the SELECT above.
COLUMNS = [
    "channel", "id", "sku", "title", "ingredient",
    "product_benefit", "product_code", "product_name", "product_group",
    "master_category", "master_subcategory", "master_pack_size", "master_uom",
    "oneds_category", "oneds_subcategory", "oneds_pack_size", "oneds_uom",
    "final_score", "llm_reasoning", "match_rank", "brand", "mapping_status",
    "mapping_id", "relationship_type", "ensemble_score", "confidence_level",
    "lexical_score", "vector_score", "approved_at",
    "review_status",   # last
]

SCOPES = ("unreviewed", "rejected", "approved", "all")


def _where(scope: str, channel: str | None, category: str | None,
           brand: str | None) -> tuple[str, dict]:
    """The WHERE clause and its parameters for one scope."""
    where = []
    params: dict[str, object] = {}

    # Brand filter, defaulting to Himalaya as the shared query does.
    if brand:
        where.append("UPPER(LTRIM(RTRIM(p.brand))) = :brand")
        params["brand"] = brand.strip().upper()

    if channel:
        where.append("p.channel = :channel")
        params["channel"] = channel
    if category:
        where.append("p.category = :category")
        params["category"] = category

    if scope == "unreviewed":
        # NOT EXISTS against the same row, as the original query writes it:
        # a row with any verdict is out, whichever channel it sits on.
        where.append("""NOT EXISTS (
            SELECT 1 FROM [staging].[vw_channel_products] AS p2
            WHERE p2.id = p.id AND p2.channel = p.channel
              AND UPPER(LTRIM(RTRIM(p2.review_status))) IN ('APPROVED','REJECTED')
        )""")
    elif scope == "rejected":
        where.append("UPPER(LTRIM(RTRIM(p.review_status))) = 'REJECTED'")
    elif scope == "approved":
        where.append("UPPER(LTRIM(RTRIM(p.review_status))) = 'APPROVED'")
    # "all" adds nothing.

    return (" WHERE " + " AND ".join(where)) if where else "", params


def count_rows(conn, *, scope: str = "unreviewed", channel: str | None = None,
               category: str | None = None, brand: str | None = "HIMALAYA") -> int:
    """How many rows the export would contain.

    Asked before building the file so the UI can say "12,431 rows" rather
    than starting a download of unknown size.
    """
    clause, params = _where(scope, channel, category, brand)
    return conn.execute(
        sa.text(f"SELECT COUNT(*) {_FROM} {clause}"), params
    ).scalar() or 0


def rows(conn, *, scope: str = "unreviewed", channel: str | None = None,
         category: str | None = None, brand: str | None = "HIMALAYA",
         limit: int | None = None):
    """Stream the export rows in SKU / match_rank order.

    Ordered exactly as the shared query orders them, so a reader diffing an
    export against their own run of that SQL sees the same sequence.
    """
    clause, params = _where(scope, channel, category, brand)
    top = f"TOP ({int(limit)})" if limit else ""
    sql = f"SELECT {top} {_SELECT} {_FROM} {clause} ORDER BY p.sku, m.match_rank"
    result = conn.execution_options(stream_results=True).execute(
        sa.text(sql), params
    )
    for row in result.mappings():
        yield dict(row)


def _writer(buf):
    """QUOTE_ALL because llm_reasoning contains commas, quotes and newlines --
    any one of which splits a row in a reader that trusts a bare delimiter."""
    return csv.DictWriter(
        buf, fieldnames=COLUMNS, extrasaction="ignore", quoting=csv.QUOTE_ALL,
        lineterminator="\r\n",
    )


def _clean(row: dict) -> dict:
    # None as empty rather than the string "None", which is what a naive
    # str() puts in the cell.
    return {k: ("" if row.get(k) is None else row.get(k)) for k in COLUMNS}


def csv_chunks(conn, **kwargs):
    """The export as CSV, yielded row by row.

    Streamed rather than returned whole: an unreviewed export across every
    channel is tens of thousands of rows carrying a paragraph of
    llm_reasoning each, and assembling that as one string before sending a
    byte would hold the whole file in memory twice.

    CSV rather than xlsx -- Excel opens it, and an xlsx writer would be a
    dependency carried for formatting nobody asked for.
    """
    buf = io.StringIO()
    writer = _writer(buf)

    writer.writeheader()
    yield buf.getvalue()
    buf.seek(0)
    buf.truncate(0)

    for row in rows(conn, **kwargs):
        writer.writerow(_clean(row))
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)


def to_csv(conn, **kwargs) -> str:
    """The whole export as one string. For tests and small scopes."""
    return "".join(csv_chunks(conn, **kwargs))


def run_scope(conn, execution_id: str) -> dict | None:
    """The channel / engine / category one run covered.

    audit.engine_run stores the SCOPE a run was launched with, not the SKUs it
    touched, so an export "for this run" is really "for what this run covered".
    The caller surfaces that distinction rather than implying a per-SKU record
    that does not exist.
    """
    row = conn.execute(
        sa.text("""
            SELECT execution_id, channel, engine, category, subcategory,
                   explicit_sku_count, to_run, processed, failed, status,
                   started_at, ended_at, triggered_by
            FROM audit.engine_run WHERE execution_id = :i
        """),
        {"i": execution_id},
    ).mappings().first()
    return dict(row) if row else None
