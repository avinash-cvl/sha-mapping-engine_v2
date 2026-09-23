"""Rejected matches: recording them, listing them, and honouring them.

WHY THIS EXISTS
---------------
Until now a steward's verdict was write-only in one direction. Approving a
match writes a crosswalk row and the engine reads it back forever (step 6B's
short-circuit). Rejecting one wrote nothing at all -- the row simply never
appeared in the crosswalk, indistinguishable from a match nobody had looked
at yet.

That loses the single most valuable signal the review process produces. An
approval says "the engine was right", which is what it is already trying to
be. A rejection says "the engine was confidently wrong", and names the exact
product code it was wrong about. Thirteen of the forty QC misses traced back
to a gate pointing at a node too small to hold an alternative -- and every one
of those would have surfaced as a rejection against the same decoy code long
before anyone ran a QC pass by hand.

NO SCHEMA CHANGE IS NEEDED
--------------------------
staging.<channel>_products already carries review_status, reviewer_comment,
reviewed_by and reviewed_at on all four channels. Only the value 'Rejected'
was never used -- today those columns hold exactly NULL or 'Approved'. So
this module writes a value into a column that already exists rather than
adding a table, which also means the steward portal sees rejections the
moment it looks for them.

WHAT A REJECTION DOES TO A RE-RUN
---------------------------------
It excludes the REJECTED MATCH, not the SKU.

scope._not_approved_clause matches anything that is not 'Approved', so a
rejected SKU stays eligible and the engine will process it again. That is
deliberate and correct: rejecting "lakme lip love -> CHERRY SHINE" is a
verdict about that pairing, not a statement that the listing is unmatchable.
What would be wrong is re-running it and landing on the same product code
again, which is why rejected (sku, product_code) pairs are surfaced to the
operator before a run rather than silently re-proposed.

Suppressing the pair inside the scorer would be the next step, and is
deliberately NOT done here: it changes what the engine returns, and every
scoring change so far has traded a handful of recovered matches for a larger
number of broken ones. That needs a regression run behind it, not a commit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import sqlalchemy as sa

from common import db_models

# The vocabulary, stated once. Compared case-insensitively everywhere because
# the steward portal and this console write through different code paths and
# 'Rejected' / 'REJECTED' must not become two different states.
REJECTED = "Rejected"
APPROVED = "Approved"


@dataclass(frozen=True)
class RejectionResult:
    channel: str
    sku: str
    product_code: str | None
    rejected_by: str
    comment: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def _rejected_clause(source):
    """Rows a steward has rejected.

    Spelled out rather than negating the approved clause: review_status is
    NULL on every unreviewed row, and NULL comparisons are NULL rather than
    false -- the same three-valued-logic trap that made an earlier count come
    back as zero for every unreviewed row in the table.
    """
    if not hasattr(source.c, "review_status"):
        return sa.false()
    return sa.func.upper(
        sa.func.ltrim(sa.func.rtrim(source.c.review_status))
    ) == REJECTED.upper()


def reject(
    conn,
    channel: str,
    sku: str,
    *,
    actor: str,
    comment: str | None = None,
) -> RejectionResult:
    """Record that the rank-1 match for this SKU is wrong.

    The product code is read from the mapping row rather than taken from the
    caller: a rejection has to name what was actually proposed, and a code
    supplied by a client is a claim about the past that nothing verifies.

    Writes in one transaction so a row can never end up marked rejected with
    no reviewer against it.
    """
    source = db_models.get_table(conn.engine, "staging", f"{channel}_products")
    mapping = db_models.get_table(conn.engine, "staging", f"{channel}_product_mapping")

    row = conn.execute(
        sa.select(source.c.id, source.c.review_status)
        .where(source.c.sku == sku)
    ).first()
    if row is None:
        raise LookupError(f"no {channel} listing with sku {sku!r}")

    # An approved row is not re-decidable from here. The crosswalk has already
    # been written and the engine short-circuits on it; flipping the source
    # row alone would leave the two disagreeing, with the crosswalk winning
    # silently on the next run.
    if (row.review_status or "").strip().upper() == APPROVED.upper():
        raise PermissionError(
            f"{sku} is steward-approved. Withdraw the approval in the review "
            f"portal first -- the crosswalk entry would otherwise still apply."
        )

    proposed = conn.execute(
        sa.select(mapping.c.product_code)
        .where(mapping.c[f"{channel}_product_id"] == row.id)
        .where(mapping.c.match_rank == 1)
    ).scalar()

    # conn.commit(), not `with conn.begin()`: these connections arrive with an
    # implicit transaction already open, and begin() on one raises "this
    # connection has already initialized a SQLAlchemy Transaction". Same
    # convention as recovery.py and run_record.py.
    conn.execute(
        sa.update(source)
        .where(source.c.id == row.id)
        .values(
            review_status=REJECTED,
            reviewed_by=actor,
            reviewed_at=datetime.now(timezone.utc),
            reviewer_comment=comment,
        )
    )
    conn.commit()

    return RejectionResult(
        channel=channel, sku=sku, product_code=proposed,
        rejected_by=actor, comment=comment,
    )


def withdraw(conn, channel: str, sku: str, *, actor: str) -> bool:
    """Undo a rejection, returning the row to unreviewed.

    Only a rejection is withdrawable here. Clearing an approval would orphan
    its crosswalk row, which this module has no business doing.
    """
    source = db_models.get_table(conn.engine, "staging", f"{channel}_products")
    result = conn.execute(
        sa.select(source.c.id, source.c.review_status).where(source.c.sku == sku)
    ).first()
    if result is None:
        raise LookupError(f"no {channel} listing with sku {sku!r}")
    if (result.review_status or "").strip().upper() != REJECTED.upper():
        return False

    conn.execute(
        sa.update(source)
        .where(source.c.id == result.id)
        .values(
            review_status=None,
            reviewed_by=actor,
            reviewed_at=datetime.now(timezone.utc),
            reviewer_comment=None,
        )
    )
    conn.commit()
    return True


def listing(
    conn,
    channel: str,
    *,
    category: str | None = None,
    engine_filter=None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Rejected listings, with what the engine had proposed for each.

    Joined on the source ROW id rather than on sku -- a sku can have more than
    one source row, and joining on sku returns the same listing once per twin.
    """
    source = db_models.get_table(conn.engine, "staging", f"{channel}_products")
    mapping = db_models.get_table(conn.engine, "staging", f"{channel}_product_mapping")

    join = source.outerjoin(
        mapping,
        sa.and_(
            mapping.c[f"{channel}_product_id"] == source.c.id,
            mapping.c.match_rank == 1,
        ),
    )
    where = [_rejected_clause(source)]
    if category:
        where.append(source.c.category == category)
    if engine_filter is not None:
        where.append(engine_filter(source))
    if q:
        like = f"%{q}%"
        where.append(sa.or_(
            source.c.sku.like(like),
            source.c.title.like(like),
            mapping.c.product_code.like(like),
            mapping.c.product_name.like(like),
        ))

    total = conn.execute(
        sa.select(sa.func.count()).select_from(join).where(*where)
    ).scalar() or 0

    rows = conn.execute(
        sa.select(
            source.c.sku, source.c.title, source.c.brand,
            source.c.category, source.c.subcategory,
            source.c.mapping_status, source.c.reviewed_by,
            source.c.reviewed_at, source.c.reviewer_comment,
            mapping.c.product_code, mapping.c.product_name,
            mapping.c.final_score, mapping.c.llm_reasoning,
        )
        .select_from(join).where(*where)
        .order_by(source.c.reviewed_at.desc())
        .offset(offset).limit(limit)
    ).mappings().all()

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "rows": [
            {
                "sku": r["sku"],
                "title": r["title"],
                "brand": r["brand"],
                "category": r["category"],
                "subcategory": r["subcategory"],
                "mapping_status": r["mapping_status"],
                "rejected_code": r["product_code"],
                "rejected_name": r["product_name"],
                "score": float(r["final_score"]) if r["final_score"] is not None else None,
                "reasoning": r["llm_reasoning"],
                "reviewed_by": r["reviewed_by"],
                "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
                "comment": r["reviewer_comment"],
            }
            for r in rows
        ],
    }


def offenders(conn, channel: str, limit: int = 20) -> list[dict]:
    """Master products rejections keep landing on.

    This is the reason the whole feature is worth having. One product code
    appearing across many rejections is the signature of a decoy node -- a
    gate target holding so few rows that everything routed there comes back
    as the same product. Counting rejections per code finds those without
    anyone running a QC pass by hand.
    """
    source = db_models.get_table(conn.engine, "staging", f"{channel}_products")
    mapping = db_models.get_table(conn.engine, "staging", f"{channel}_product_mapping")

    join = source.join(
        mapping,
        sa.and_(
            mapping.c[f"{channel}_product_id"] == source.c.id,
            mapping.c.match_rank == 1,
        ),
    )
    rows = conn.execute(
        sa.select(
            mapping.c.product_code,
            mapping.c.product_name,
            sa.func.count().label("rejections"),
        )
        .select_from(join)
        .where(_rejected_clause(source))
        .where(mapping.c.product_code.isnot(None))
        .group_by(mapping.c.product_code, mapping.c.product_name)
        .order_by(sa.func.count().desc())
        .limit(limit)
    ).all()
    return [
        {"product_code": r[0], "product_name": r[1], "rejections": r[2]}
        for r in rows
    ]
