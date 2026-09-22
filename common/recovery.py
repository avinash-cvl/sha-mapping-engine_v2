"""Reset rows a run left in 'Failed' so they can be picked up again.

WHY THIS IS A SEPARATE, EXPLICIT ACTION

A worker killed mid-SKU -- overwhelmingly SQL 40001 deadlock victims under
parallel load -- leaves its source row marked 'Failed'. Step 6 selects only
'PENDING', so a plain re-run skips those rows silently: the operator sees a
clean second run and never learns that 24 SKUs were never processed. This
happened twice in one day before it was noticed.

The fix is deliberately NOT to make the engine re-select 'Failed' rows on its
own. 'Failed' also covers genuine, repeatable errors, and a run that quietly
retries them would loop on the same fault and bury it. So the engine keeps
its narrow contract -- PENDING only -- and recovery is a decision somebody
makes, with the count in front of them.

WHAT IT DOES NOT TOUCH

Steward-approved rows, always and everywhere. An approved row is a governed
answer; nothing here may hand it back to the engine. That exclusion is not a
flag a caller can pass.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

import sqlalchemy as sa

from common import db, db_models
from common.scope import _brand_clause, _not_approved_clause, _source_table


@dataclass(frozen=True)
class ResetResult:
    channel: str
    pipeline: str | None
    category: str | None
    subcategory: str | None
    rows_found: int      # Failed rows matching the scope
    rows_reset: int      # actually flipped to PENDING
    skus: list[str]      # what was reset, capped for display

    def as_dict(self) -> dict:
        return asdict(self)


def _scoped(source, pipeline, category, subcategory, skus):
    """The Failed-row selection, shared by preview and apply so the number
    shown and the number changed cannot drift apart."""
    q = sa.and_(
        source.c.mapping_status == "Failed",
        _not_approved_clause(source),
    )
    if pipeline:
        q = sa.and_(q, _brand_clause(source, pipeline))
    if category:
        q = sa.and_(q, source.c.category == category)
    if subcategory:
        q = sa.and_(q, source.c.subcategory == subcategory)
    if skus:
        q = sa.and_(q, source.c.sku.in_(skus))
    return q


def preview(
    conn: db.Connection,
    channel: str,
    pipeline: str | None = None,
    category: str | None = None,
    subcategory: str | None = None,
    skus: list[str] | None = None,
    sample: int = 50,
) -> ResetResult:
    """What a reset over this scope WOULD change. Reads only."""
    schema, name = _source_table(channel).split(".", 1)
    source = db_models.get_table(conn.engine, schema, name)
    where = _scoped(source, pipeline, category, subcategory, skus)

    found = conn.execute(
        sa.select(sa.func.count()).select_from(source).where(where)
    ).scalar() or 0

    listed = [
        r[0] for r in conn.execute(
            sa.select(source.c.sku).where(where).order_by(source.c.id).limit(sample)
        ).all()
    ]

    return ResetResult(
        channel=channel, pipeline=pipeline, category=category,
        subcategory=subcategory, rows_found=found, rows_reset=0, skus=listed,
    )


def reset_failed(
    conn: db.Connection,
    channel: str,
    pipeline: str | None = None,
    category: str | None = None,
    subcategory: str | None = None,
    skus: list[str] | None = None,
    actor: str | None = None,
) -> ResetResult:
    """Flip Failed rows back to PENDING so the next run picks them up.

    Clears match_rank and mapping_id alongside the status: they point at the
    partial result of the run that died, and leaving them behind makes the
    row look mapped while it is queued to be mapped again.

    Writes an audit.activity_log entry naming the actor, in the same
    transaction as the reset -- a reset changes what the engine will process,
    so it belongs in the same trail as a steward decision rather than
    happening invisibly. If the audit write fails the reset fails with it,
    rather than leaving rows quietly re-queued by nobody.
    """
    schema, name = _source_table(channel).split(".", 1)
    source = db_models.get_table(conn.engine, schema, name)
    where = _scoped(source, pipeline, category, subcategory, skus)

    before = preview(conn, channel, pipeline, category, subcategory, skus)
    if before.rows_found == 0:
        return before

    values = {"mapping_status": "PENDING"}
    for optional in ("match_rank", "mapping_id"):
        if hasattr(source.c, optional):
            values[optional] = None

    result = conn.execute(sa.update(source).where(where).values(**values))

    # audit.activity_log carries a CHECK constraint isjson(input) = 1, so the
    # payload is a JSON document, not prose -- matching the shape
    # STAGING_REVIEW_UPDATED and CROSSWALK_UPSERT already write.
    activity = db_models.get_table(conn.engine, "audit", "activity_log")
    conn.execute(
        sa.insert(activity).values(
            entity=f"staging.{channel}_products",
            entity_key=(
                f"{pipeline or 'all'}/{category or 'all'}/{subcategory or 'all'}"
            )[:1000],
            action="FAILED_ROWS_RESET",
            input=json.dumps(
                {
                    "channel": channel,
                    "pipeline": pipeline,
                    "category": category,
                    "subcategory": subcategory,
                    "rows_found": before.rows_found,
                    "rows_reset": result.rowcount,
                    "sample_skus": before.skus[:10],
                }
            ),
            logged_by=actor or "system",
        )
    )

    # One transaction: the rows go back to PENDING and the trail saying who
    # did it lands together, or neither does.
    conn.commit()

    return ResetResult(
        channel=channel, pipeline=pipeline, category=category,
        subcategory=subcategory, rows_found=before.rows_found,
        rows_reset=result.rowcount, skus=before.skus,
    )
