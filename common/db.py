"""Query functions over the raw/staging/app/config/audit schema, built on
SQLAlchemy Core against tables reflected by db_models.py. Every function
takes a `Connection` (like the old pyodbc-based db.py took a
`pyodbc.Connection`) and a plain dict/dataclass -- no ORM Session, no
behavior classes, just functions over typed data, matching the house style.

The one exception is the embedding column: SQL Server 2025's VECTOR type
isn't understood by SQLAlchemy's mssql dialect (see db_models.py's
docstring), so embedding reads/writes stay raw parameterized SQL via
`conn.exec_driver_sql()`, same CAST(...AS VECTOR(n)) workaround as before.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import sqlalchemy as sa

import common.config as C
from common import db_models
from common.models import MatchResult

Connection = sa.Connection


def get_connection() -> Connection:
    engine = db_models.get_engine()
    return engine.connect()


def vector_literal(values: list[float]) -> str:
    """JSON-encode an embedding for CAST(? AS VECTOR(n)) in raw SQL."""
    return json.dumps(values)


def _inserted_id(result: sa.CursorResult[Any]) -> int:
    """Every insert helper in this module inserts exactly one row into a
    single-column IDENTITY primary key table, so inserted_primary_key is
    always populated -- the assert documents that invariant for mypy."""
    key = result.inserted_primary_key
    assert key is not None
    return int(key[0])


# ---------------------------------------------------------------------------
# audit.pipeline_execution_log -- one row per flow run. execution_id is the
# run_id threaded through checkpoint.py and every write below; batch_id is
# kept 1:1 with execution_id (one flow run = one batch, per the meeting
# notes' "track with batch id").
# ---------------------------------------------------------------------------

def create_run(conn: Connection, pipeline_name: str, source_file_path: str) -> str:
    """Generates the run_id client-side (rather than SELECTing back a
    server-generated NEWID()) so the SAME value can be used as both
    execution_id and batch_id in one INSERT -- "one flow run = one batch",
    per the meeting notes' "track with batch id", with a single id to
    thread through checkpoint.py and every raw/staging/crosswalk write."""
    run_id = str(uuid.uuid4())
    table = db_models.get_table(conn.engine, "audit", "pipeline_execution_log")
    conn.execute(
        table.insert().values(
            execution_id=run_id,
            batch_id=run_id,
            pipeline_name=pipeline_name,
            execution_start_time=sa.func.sysdatetime(),
            status="running",
            source_file_path=source_file_path,
        )
    )
    conn.commit()
    return run_id


def finish_run(
    conn: Connection,
    run_id: str,
    status: str,
    rows_processed: int,
    error: str | None = None,
) -> None:
    table = db_models.get_table(conn.engine, "audit", "pipeline_execution_log")
    conn.execute(
        table.update()
        .where(table.c.execution_id == run_id)
        .values(
            status=status,
            execution_end_time=sa.func.sysdatetime(),
            total_records_loaded=rows_processed,
            error_message=error,
        )
    )
    conn.commit()


# ---------------------------------------------------------------------------
# raw.* -- landing tables. Inserted one row at a time so each row's IDENTITY
# id is available immediately for the staging FK -- fine at this project's
# current data scale; batch/executemany with id capture is a later
# optimization if a real ~41k-row run needs it (see stage_ingest.py's
# existing df.iterrows() note).
# ---------------------------------------------------------------------------

def insert_raw_himalaya_row(conn: Connection, batch_id: str, row: dict[str, Any]) -> int:
    table = db_models.get_table(conn.engine, "raw", "himalaya_products")
    result = conn.execute(table.insert().values(batch_id=batch_id, sync_status="COMPLETED", **row))
    return _inserted_id(result)


def insert_raw_oneds_row(conn: Connection, batch_id: str, row: dict[str, Any]) -> int:
    table = db_models.get_table(conn.engine, "raw", "oneds_products")
    result = conn.execute(table.insert().values(batch_id=batch_id, sync_status="COMPLETED", **row))
    return _inserted_id(result)


# ---------------------------------------------------------------------------
# staging.himalaya_products -- Himalaya's own normalized catalog (one table,
# not per-channel). embedding is written separately by stage_candidates.py.
# ---------------------------------------------------------------------------

def insert_staging_himalaya_row(conn: Connection, batch_id: str, bronze_product_id: int, row: dict[str, Any]) -> int:
    table = db_models.get_table(conn.engine, "staging", "himalaya_products")
    result = conn.execute(
        table.insert().values(batch_id=batch_id, bronze_product_id=bronze_product_id, sync_status="PENDING", **row)
    )
    return _inserted_id(result)


def write_embedding(conn: Connection, staging_himalaya_id: int, embedding: list[float]) -> None:
    conn.exec_driver_sql(
        f"UPDATE staging.himalaya_products SET embedding = CAST(CAST(? AS NVARCHAR(MAX)) AS VECTOR({C.EMBED_DIM})), "
        "sync_status = 'COMPLETED' WHERE id = ?",
        (vector_literal(embedding), staging_himalaya_id),
    )


# SQL Server hard-caps bound parameters at 2100 per query -- a single-column
# IN() can safely use most of that budget (unlike stage_deterministic.py's
# multi-table UNION, which needs a smaller per-query share).
_IN_CLAUSE_CHUNK_SIZE = 2000


def _chunks(items: list[int], size: int) -> list[list[int]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def rows_missing_embedding(conn: Connection, staging_himalaya_ids: list[int]) -> set[int]:
    """Which of these staging.himalaya_products ids still have no embedding
    -- used to skip re-embedding rows that came from load_master_from_staging
    (already embedded by the original restore/ingest)."""
    if not staging_himalaya_ids:
        return set()
    table = db_models.get_table(conn.engine, "staging", "himalaya_products")
    missing: set[int] = set()
    for chunk in _chunks(staging_himalaya_ids, _IN_CLAUSE_CHUNK_SIZE):
        rows = conn.execute(
            sa.select(table.c.id).where(table.c.id.in_(chunk), table.c.embedding.is_(None))
        ).all()
        missing.update(row.id for row in rows)
    return missing


def semantic_search(conn: Connection, query_vector: list[float], k: int) -> list[tuple[str, float]]:
    """Top-k product_codes by cosine distance against staging.himalaya_products,
    via SQL Server 2025's native VECTOR_DISTANCE(). Same raw-SQL cast
    workaround as the old dbo.master_products version (see
    stage_candidates.py's upsert_master_products docstring)."""
    cursor = conn.exec_driver_sql(
        f"""
        SELECT TOP ({k}) product_code,
               VECTOR_DISTANCE('cosine', embedding, CAST(CAST(? AS NVARCHAR(MAX)) AS VECTOR({C.EMBED_DIM}))) AS distance
        FROM staging.himalaya_products
        WHERE embedding IS NOT NULL AND product_code IS NOT NULL
        ORDER BY distance ASC
        """,
        (vector_literal(query_vector),),
    )
    return [(str(row.product_code), 1.0 - float(row.distance)) for row in cursor]


# ---------------------------------------------------------------------------
# staging.{channel}_products -- one row per source SKU, per channel.
# ---------------------------------------------------------------------------

def insert_staging_channel_product_row(
    conn: Connection, channel_tables: db_models.ChannelTables, batch_id: str, row: dict[str, Any]
) -> int:
    result = conn.execute(channel_tables.products.insert().values(batch_id=batch_id, **row))
    return _inserted_id(result)


# ---------------------------------------------------------------------------
# staging.{channel}_product_mapping -- every scored candidate, always
# written (audit trail), regardless of whether it clears auto-approve.
# ---------------------------------------------------------------------------

def _none_if_nan(value: float | None) -> float | None:
    if value is None:
        return None
    return None if value != value else value  # NaN is the only float that != itself


def _category_tag(r: MatchResult) -> str | None:
    """Human- and grep-readable summary of the V2 category decision, e.g.
    '[master:FACE WASH/FACE WASH conf=0.94]'.

    Prefixed onto llm_reasoning so the decision is visible to a steward on
    every channel today, whether or not the dedicated columns from
    sql/007_category_resolution_columns.sql have been applied yet.

    In practice only the gated branch is reached from oneds_master: rows
    the resolver closes as terminal have no candidate, and product_code is
    NOT NULL, so they never produce a mapping row (see
    step_6c_apply_category_shortcircuit). The terminal branch is kept for
    any caller that does persist such a row."""
    if r.master_category:
        tag = f"[master:{r.master_category}/{r.master_subcategory}"
        if r.category_confidence is not None:
            tag += f" conf={r.category_confidence:.2f}"
        return tag + "]"
    if r.resolution_method in ("category_no_equivalent", "category_unclassified"):
        terminal = r.resolution_method.removeprefix("category_")
        return f"[category:{terminal}]"
    return None


def _mapping_row(
    batch_id: str,
    source_row_id: int,
    r: MatchResult,
    mapping_table: sa.Table | None = None,
) -> dict[str, Any]:
    scores = r.scores

    final_score = _none_if_nan(r.llm_confidence)

    ensemble_score = _none_if_nan(scores.ensemble) if scores else None

    # final_score is the SIMILARITY of this candidate -- how close it is to
    # the listing. It is deliberately NOT forced to agree with the tier.
    #
    # The two are different questions and both matter to a steward:
    #
    #   ensemble/final : "how close is this candidate?"   0.96 -- same product
    #                    line, same 4.5g size, wrong pack count
    #   confidence_level: "did we conclude it IS the product?"  No
    #
    # A "Pack of 5" listing with no pack-of-5 in the master is correctly
    # rejected, but 7005170 (LITCHI SHINE LIP CARE 4.5G PACK OF 2) is still a
    # very near miss and a steward may well approve it. Zeroing the score
    # there would say "nothing like this exists", which is false and strictly
    # less useful than the truth: "we found something very close, but it is
    # not the same sellable unit."
    #
    # So the score keeps its meaning, and the tier carries the verdict. What
    # must NOT happen is the portal reading the similarity number as
    # confidence -- see the note in the commit and the review_status column.
    #
    # The max() that used to sit here is still wrong for a different reason:
    # it let a REJECTED candidate's ensemble overwrite a judge confidence that
    # was deliberately lower, so a demoted row reported a higher number than
    # the judge assigned. Keep the ensemble as the reported similarity, but
    # never let it masquerade as the judge's confidence.
    # Rejections (judge returned pick=null, confidence 0.0) report the
    # candidate's own similarity rather than the judge's 0.0 -- the 0.0 is a
    # verdict about the MATCH, not a measurement of the CANDIDATE, and the
    # tier already carries the verdict.
    if final_score is None or (r.llm_pick == "" and ensemble_score is not None):
        final_score = ensemble_score

    # The category decision rides in llm_reasoning so it is visible without a
    # schema change; the structured columns below are written as well, but
    # only once they exist.
    llm_reasoning = r.llm_reason
    tag = _category_tag(r)
    if tag:
        detail = r.llm_reason or r.category_evidence or ""
        llm_reasoning = f"{tag} {detail}".strip()

    row = {
        "batch_id": batch_id,
        "match_rank": r.rank,
        "source_sku": r.source.sku,
        "product_code": r.candidate.product_code if r.candidate else None,
        "product_name": r.candidate.product_name if r.candidate else None,
        "relationship_type": r.source.match_type,
        "lexical_score": _none_if_nan(scores.lexical) if scores else None,
        "vector_score": _none_if_nan(scores.semantic) if scores else None,
        "category_score": _none_if_nan(scores.category) if scores else None,
        "pack_score": _none_if_nan(scores.pack) if scores else None,
        "form_factor_score": _none_if_nan(scores.type_align) if scores else None,
        "ensemble_score": _none_if_nan(scores.ensemble) if scores else None,
        "confidence_level": r.confidence_tier,
        "llm_reasoning": llm_reasoning,
        "final_score": final_score,
    }

    # Structured category columns, written only where the channel's mapping
    # table actually has them -- an in-memory check against the reflected
    # metadata, no per-row round trip. DDL is applied out of band (see
    # sql/007_category_resolution_columns.sql); this repo never issues it.
    # Note reflect_metadata() memoizes per process, so a table altered while
    # a run is in flight stays invisible until the next run.
    if mapping_table is not None:
        for column, value in (
            ("master_category", r.master_category),
            ("master_subcategory", r.master_subcategory),
            ("category_confidence", _none_if_nan(r.category_confidence)),
            ("category_evidence", r.category_evidence),
        ):
            if value is not None and column in mapping_table.c:
                row[column] = value

    return row


def write_staging_mapping(
    conn: Connection, channel_tables: db_models.ChannelTables, batch_id: str, results: list[MatchResult]
) -> None:
    """Writes every ranked candidate (see stage_disposition.disposition_all())
    into staging.{channel}_product_mapping -- the audit trail every source
    row leaves behind, independent of auto-approve/crosswalk.

    Idempotent per batch_id: clears any rows already written for this batch
    before inserting -- a real bug (found live) otherwise doubles this
    table when a run's persist step is replayed from a checkpoint after a
    partial failure (this write already committed once, then the same
    batch got re-persisted to fix a later, unrelated write). Unlike
    push_to_crosswalk, this table isn't safe to skip-if-present, since a
    replay may carry corrected data -- clear-then-insert instead."""
    #conn.execute(channel_tables.mapping.delete().where(channel_tables.mapping.c.batch_id == batch_id))
    for r in results:
        row = _mapping_row(batch_id, r.source.source_row_id, r)
        row[channel_tables.product_id_column] = r.source.source_row_id
        conn.execute(channel_tables.mapping.insert().values(**row))
    conn.commit()


def persist_sku_disposition(
    conn: Connection,
    channel_tables: db_models.ChannelTables,
    batch_id: str,
    results: list[MatchResult],
    mapping_status: str,
    audit_entries: list[dict[str, Any]],
) -> None:
    """Atomically replace one source SKU's persisted disposition.

    A disposition contains the rank-1 decision plus its rank-2/rank-3
    alternatives.  Replacing only rows for this source product and batch
    makes retrying a partially completed run safe without disturbing SKUs
    that were already committed.  Mapping rows, the source product status,
    and the SKU's LLM audit rows share one transaction: callers observe all
    of them, or none of them.
    """
    if not results:
        return

    product_id_column = channel_tables.product_id_column
    product_id = results[0].source.source_row_id
    if any(result.source.source_row_id != product_id for result in results):
        raise ValueError("persist_sku_disposition requires results for exactly one source product")

    try:
        # Delete EVERY existing mapping row for this source product, not just
        # the ones carrying this run's batch_id.
        #
        # The batch_id predicate used to be part of this WHERE clause, which
        # meant the delete could never match a previous run: each run mints a
        # new batch_id, so re-running a SKU APPENDED a second full set of
        # rank-1/2/3 rows instead of replacing the old ones. Measured on a
        # re-run of three swiggy SKUs: 6 mapping rows each, with two
        # different rank-1 candidates from two different runs, and nothing in
        # the table marking which one was current. A steward reviewing that
        # is choosing between a stale answer and a fresh one at random.
        #
        # A source product has exactly one current disposition, so scoping
        # the delete to the product alone is what "replace this SKU's
        # disposition" has to mean. Retry safety is unaffected: this still
        # only ever touches rows for the one product being written, inside
        # the same transaction as the insert.
        conn.execute(
            channel_tables.mapping.delete().where(
                getattr(channel_tables.mapping.c, product_id_column) == product_id,
            )
        )
        for result in results:
            row = _mapping_row(
                batch_id, product_id, result, mapping_table=channel_tables.mapping
            )
            row[product_id_column] = product_id
            conn.execute(channel_tables.mapping.insert().values(**row))

        mark_product_status(conn, channel_tables, product_id, mapping_status)

        if audit_entries:
            audit_table = db_models.get_table(conn.engine, "audit", "llm_call_log")
            sku = results[0].source.sku
            conn.execute(
                audit_table.delete().where(
                    audit_table.c.run_id == batch_id,
                    audit_table.c.sku == sku,
                )
            )
            _write_llm_audit(conn, audit_table, batch_id, audit_entries)

        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# app.{channel}_crosswalk -- the golden record. push_to_crosswalk() is the
# single idempotency-guarded entry point used by both the pipeline's
# auto-approve path (flow.py) and the portal's steward-approve path
# (sha-portal-api) -- "check SKU exists in crosswalk before pushing".
# ---------------------------------------------------------------------------

def push_to_crosswalk(
    conn: Connection,
    channel_tables: db_models.ChannelTables,
    batch_id: str,
    result: MatchResult,
    match_status: str,
    reviewed_by: str | None = None,
) -> bool:
    """Inserts one rank=1 crosswalk row for result.source, unless that
    product already has a rank=1 row (the idempotency guard from the
    meeting notes). Returns whether a row was actually inserted."""
    target = channel_tables.crosswalk if result.source.match_type == "Catalog Match" else channel_tables.crosswalk_competitor
    product_id_column = channel_tables.product_id_column
    product_id = result.source.source_row_id

    already_present = conn.execute(
        sa.select(sa.literal(1)).select_from(target).where(
            getattr(target.c, product_id_column) == product_id, target.c.match_rank == 1
        )
    ).first()
    if already_present is not None:
        return False

    scores = result.scores
    source = result.source
    row: dict[str, Any] = {
        product_id_column: product_id,
        "match_rank": 1,
        "batch_id": batch_id,
        "sku": source.sku,
        "product_code": result.candidate.product_code if result.candidate else None,
        "lexical_score": _none_if_nan(scores.lexical) if scores else None,
        "vector_score": _none_if_nan(scores.semantic) if scores else None,
        "category_score": _none_if_nan(scores.category) if scores else None,
        "pack_score": _none_if_nan(scores.pack) if scores else None,
        "form_factor_score": _none_if_nan(scores.type_align) if scores else None,
        "ensemble_score": _none_if_nan(scores.ensemble) if scores else None,
        "llm_score": _none_if_nan(result.llm_confidence),
        "llm_reason": result.llm_reason,
        "match_status": match_status,
        "reviewed_by": reviewed_by,
        "title": source.title,
        "brand": source.brand,
        "category": source.category,
        "subcategory": source.subcategory,
        "pack_size": source.pack_value,
        "uom": source.pack_unit,
        "variant": source.variant,
        "ingredient": source.ingredient,
        "product_benefit": source.benefit,
    }
    conn.execute(target.insert().values(**row))
    mark_product_status(conn, channel_tables, product_id, "AUTO_MATCHED" if match_status == "auto_approved" else "APPROVED")
    conn.commit()
    return True


def mark_product_status(conn: Connection, channel_tables: db_models.ChannelTables, product_id: int, status: str) -> None:
    """Updates staging.{channel}_products.mapping_status -- the PENDING /
    AUTO_MATCHED / APPROVED / REJECTED enum from the meeting notes, kept on
    the product row itself so "is this SKU still awaiting steward action"
    is a single-column read, not a join/derive."""
    conn.execute(
        channel_tables.products.update()
        .where(channel_tables.products.c.id == product_id)
        .values(mapping_status=status)
    )


def write_llm_audit(conn: Connection, run_id: str, entries: list[dict[str, Any]]) -> None:
    """Writes to audit.llm_call_log -- the colleague's dump had no LLM-audit
    equivalent (config.token_normalization is a different concern: a
    lexicon, not a call log), so this table was added alongside the restored
    schema in 006_channel_schema.sql, replacing dbo.llm_audit (dropped in
    008)."""
    table = db_models.get_table(conn.engine, "audit", "llm_call_log")
    _write_llm_audit(conn, table, run_id, entries)
    conn.commit()


def _write_llm_audit(
    conn: Connection, table: sa.Table, run_id: str, entries: list[dict[str, Any]]
) -> None:
    """Insert LLM audit rows without committing.

    ``persist_sku_disposition`` uses this internal form so audit rows can be
    committed together with the mapping rows and mapping_status update.
    """
    for e in entries:
        conn.execute(
            table.insert().values(
                run_id=run_id,
                sku=e.get("sku"),
                call_type=e.get("call_type"),
                model=e.get("model"),
                system_prompt=e.get("system_prompt"),
                request_messages=e.get("request_messages"),
                response_text=e.get("response_text"),
                latency_ms=e.get("latency_ms"),
                success=bool(e.get("success", True)),
                error_message=e.get("error_message"),
            )
        )
