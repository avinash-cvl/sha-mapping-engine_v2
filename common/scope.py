"""Scope resolution: what a run WOULD process, without processing it.

This is the count the console shows before the operator confirms, and it is
deliberately the same code the run itself selects with -- a preview computed
by a second, parallel implementation would eventually disagree with the
selection, and a preview that lies is worse than no preview.

The numbers matter because they are not interchangeable. A single "552 rows"
total hid a 2x duplicate ingest for weeks; the same scope reported as
274 to run / 88 approved-skipped / 0 already mapped makes the shape visible.
So resolve() never returns one figure -- it returns the breakdown.

Used by:
  * oneds_master / oneds_competitor batch_flow.main()  -- CLI preview
  * routers/engine.py                                  -- console preview
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import sqlalchemy as sa

import common.config as C
from common import db, db_models

# The two engines, named the way the operator names them rather than by
# module. "himalaya" maps Himalaya's own listings to the master; "competitor"
# maps everyone else's to the nearest Himalaya equivalent.
ENGINES = ("himalaya", "competitor")


@dataclass(frozen=True)
class ScopeCounts:
    """What a run over this scope would do. Every field is a separate answer
    to a separate question -- see the module docstring on why these are not
    summed into one."""

    channel: str
    engine: str
    category: str | None
    subcategory: str | None

    scoped_total: int       # rows matching channel+engine+category filters
    to_run: int             # PENDING, not steward-approved -> the engine works these
    approved_skipped: int   # steward-approved; never re-run, never overwritten
    already_mapped: int     # carry a mapping_status other than PENDING/Failed
    failed_resettable: int  # Failed -- NOT picked up by a plain re-run (see below)

    source_row_count: int   # whole-table row count, pins the input population

    def as_dict(self) -> dict:
        return asdict(self)


def _source_table(channel: str) -> str:
    return f"staging.{channel}_products"


def _brand_clause(source, engine: str):
    """The one clause that separates the two engines.

    Expressed as membership / non-membership of C.HIMALAYA_BRANDS rather than
    an explicit competitor list: the competitor set is open-ended, and a brand
    the ingest has not seen before should flow through the competitor engine
    by default instead of being silently dropped.
    """
    if engine == "himalaya":
        return source.c.brand.in_(C.HIMALAYA_BRANDS)
    if engine == "competitor":
        return ~source.c.brand.in_(C.HIMALAYA_BRANDS)
    raise ValueError(f"engine must be one of {ENGINES}, got {engine!r}")


def _approved_clause(source):
    """Rows a steward has approved. None when the table has no review_status
    column at all, in which case nothing is excluded -- the pre-existing
    behaviour."""
    if not hasattr(source.c, "review_status"):
        return None
    return sa.func.upper(
        sa.func.ltrim(sa.func.rtrim(source.c.review_status))
    ) == "APPROVED"


def _not_approved_clause(source):
    """Rows a steward has NOT approved.

    Deliberately NOT sa.not_(_approved_clause(...)). review_status is NULL on
    every row nobody has reviewed, UPPER(TRIM(NULL)) = 'APPROVED' evaluates to
    NULL rather than false, and NOT(NULL) is still NULL -- so the negation
    filters out every unreviewed row and the count silently comes back 0.
    Spelled out as "IS NULL OR <> 'APPROVED'", which is also exactly how
    batch_flow_steps.py writes the same filter at selection time.
    """
    if not hasattr(source.c, "review_status"):
        return sa.true()
    return sa.or_(
        source.c.review_status.is_(None),
        sa.func.upper(
            sa.func.ltrim(sa.func.rtrim(source.c.review_status))
        ) != "APPROVED",
    )


def resolve(
    conn: db.Connection,
    channel: str,
    engine: str,
    category: str | None = None,
    subcategory: str | None = None,
    skus: list[str] | None = None,
) -> ScopeCounts:
    """Count what a run over this scope would process. Reads only."""
    table = _source_table(channel)
    schema, name = table.split(".", 1)
    source = db_models.get_table(conn.engine, schema, name)

    base = sa.select(sa.func.count()).select_from(source).where(
        _brand_clause(source, engine)
    )
    if category:
        base = base.where(source.c.category == category)
    if subcategory:
        base = base.where(source.c.subcategory == subcategory)
    if skus:
        base = base.where(source.c.sku.in_(skus))

    approved = _approved_clause(source)
    not_approved = _not_approved_clause(source)

    scoped_total = conn.execute(base).scalar() or 0

    to_run = conn.execute(
        base.where(source.c.mapping_status == "PENDING").where(not_approved)
    ).scalar() or 0

    approved_skipped = 0 if approved is None else (
        conn.execute(base.where(approved)).scalar() or 0
    )

    # "Already mapped" is anything the engine has finished with. Failed is
    # deliberately excluded and counted separately below -- it is neither
    # done nor, crucially, re-runnable.
    already_mapped = conn.execute(
        base.where(source.c.mapping_status.notin_(["PENDING", "Failed"]))
            .where(not_approved)
    ).scalar() or 0

    # A worker that dies (SQL 40001 deadlock, say) leaves its row marked
    # 'Failed', not 'PENDING'. Step 6 only ever selects PENDING, so a plain
    # re-run SKIPS these silently -- they need an explicit reset. Surfacing
    # the count is what turns that from a trap into a visible, actionable
    # number.
    failed_resettable = conn.execute(
        base.where(source.c.mapping_status == "Failed").where(not_approved)
    ).scalar() or 0

    # Whole-table count, unfiltered: pins the input population so a later
    # comparison spanning an ingest change is detectable rather than
    # silently misleading.
    source_row_count = conn.execute(
        sa.select(sa.func.count()).select_from(source)
    ).scalar() or 0

    return ScopeCounts(
        channel=channel,
        engine=engine,
        category=category,
        subcategory=subcategory,
        scoped_total=scoped_total,
        to_run=to_run,
        approved_skipped=approved_skipped,
        already_mapped=already_mapped,
        failed_resettable=failed_resettable,
        source_row_count=source_row_count,
    )


def resolve_both(
    conn: db.Connection,
    channel: str,
    category: str | None = None,
    subcategory: str | None = None,
    skus: list[str] | None = None,
) -> list[ScopeCounts]:
    """Both engines over the same scope, as two separate results.

    Never summed. The two sides are routinely asymmetric -- zepto lip balms
    is 4 Himalaya rows against 274 competitor rows -- and a single total
    would hide exactly the shape an operator needs to see before confirming.
    """
    return [
        resolve(conn, channel, p, category, subcategory, skus) for p in ENGINES
    ]


def groups(
    conn: db.Connection,
    channel: str,
    engine: str,
    category: str | None = None,
) -> list[dict]:
    """Per (category, subcategory) breakdown of runnable rows.

    A run spans one category but many sub-category groups, and outcome rates
    differ sharply between them, so the group is the unit worth reporting
    against -- not the run as a whole.
    """
    table = _source_table(channel)
    schema, name = table.split(".", 1)
    source = db_models.get_table(conn.engine, schema, name)

    not_approved = _not_approved_clause(source)

    q = (
        sa.select(
            source.c.category,
            source.c.subcategory,
            sa.func.count().label("total"),
            sa.func.sum(
                sa.case((source.c.mapping_status == "PENDING", 1), else_=0)
            ).label("pending"),
        )
        .where(_brand_clause(source, engine))
        .where(not_approved)
        .group_by(source.c.category, source.c.subcategory)
        .order_by(sa.func.count().desc())
    )
    if category:
        q = q.where(source.c.category == category)

    return [
        {
            "category": r[0],
            "subcategory": r[1],
            "total": r[2],
            "pending": int(r[3] or 0),
        }
        for r in conn.execute(q).all()
    ]
