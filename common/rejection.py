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

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import sqlalchemy as sa

from common import db_models

# The vocabulary, stated once. Compared case-insensitively everywhere because
# the steward portal and this console write through different code paths and
# 'Rejected' / 'REJECTED' must not become two different states.
REJECTED = "Rejected"
APPROVED = "Approved"


def _himalaya_brands() -> set[str]:
    """The brand set, lower-cased, read at call time.

    Imported inside the function because common.config reads os.environ at
    import time and this module is imported early -- taking a copy at module
    scope would freeze whatever the environment looked like then.
    """
    import common.config as C
    return {b.strip().lower() for b in C.HIMALAYA_BRANDS}


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


def _approved_clause(source):
    """Rows a steward has approved. Mirrors scope._approved_clause -- kept
    local rather than imported so this module has no dependency on scope.py,
    which itself imports rejection. Same wording, so the two can never
    silently disagree about what "approved" means."""
    if not hasattr(source.c, "review_status"):
        return sa.false()
    return sa.func.upper(
        sa.func.ltrim(sa.func.rtrim(source.c.review_status))
    ) == APPROVED.upper()


def _not_approved_clause(source):
    """Rows a steward has NOT approved -- includes rejected AND unreviewed.

    Same wording as scope._not_approved_clause (not imported, for the same
    reason _approved_clause above isn't): NULL is not 'APPROVED', so this is
    spelled out as IS NULL OR <> 'APPROVED' rather than negating the approved
    clause, which the three-valued-logic trap would silently turn into
    "excludes every unreviewed row".
    """
    if not hasattr(source.c, "review_status"):
        return sa.true()
    return sa.or_(
        source.c.review_status.is_(None),
        sa.func.upper(
            sa.func.ltrim(sa.func.rtrim(source.c.review_status))
        ) != APPROVED.upper(),
    )


# Scopes a reset/listing/preview can be parameterized over. "rejected" is the
# original, narrower scope every existing caller still gets by default;
# "non_approved" is the new, wider one the review screen needs (unreviewed +
# rejected, but never a steward-approved row -- reset_rejected's guarantee
# that approved rows are never touched holds for both).
_SCOPE_CLAUSES = {
    "rejected": _rejected_clause,
    "non_approved": _not_approved_clause,
}

# Applied to every preview_reset/reset_rejected WHERE unconditionally, in
# addition to whatever _scope_clause(scope) already excludes. An approval
# writes a crosswalk row that step 6B short-circuits on forever -- deleting
# its mapping row here would leave the crosswalk and the mapping table
# disagreeing, silently, until the next run "fixes" it back. That is not a
# failure this function gets to risk on a scope string being right, so the
# exclusion is spelled out here a second time rather than trusted to
# _SCOPE_CLAUSES alone.
NOT_APPROVED_GUARD = _not_approved_clause


def _scope_clause(scope: str):
    try:
        return _SCOPE_CLAUSES[scope]
    except KeyError:
        raise ValueError(f"scope must be one of {tuple(_SCOPE_CLAUSES)}, got {scope!r}") from None


def _engine_clause(source, engine: str | None):
    """Rows belonging to one engine, by the same brand-membership rule
    listing()/review_listing() use to label a row "himalaya" or "competitor".

    Not imported from scope.py: scope.py imports this module, so importing
    scope back would be circular. engine=None matches everything -- the
    unfiltered case every existing caller of counts() still gets.
    """
    if engine is None:
        return sa.true()
    himalaya = _himalaya_brands()
    if not hasattr(source.c, "brand"):
        return sa.true()
    in_himalaya = sa.func.lower(sa.func.ltrim(sa.func.rtrim(source.c.brand))).in_(himalaya)
    if engine == "himalaya":
        return in_himalaya
    if engine == "competitor":
        return sa.or_(source.c.brand.is_(None), sa.not_(in_himalaya))
    raise ValueError(f"engine must be 'himalaya' or 'competitor', got {engine!r}")


def counts(conn, channel: str, engine: str | None = None) -> dict:
    """Approved / rejected / non-approved totals for one channel.

    non_approved is NOT approved_total's complement restricted to
    "unreviewed" -- it is everything that isn't approved, rejected rows
    included, matching _not_approved_clause exactly so this number and the
    engine's own selection count never disagree. pending is the number of
    those that are also untouched by review (NULL review_status), which is
    the more useful figure to show next to "rejected" on a three-way tile.

    `engine` narrows every tile to "himalaya" or "competitor" rows only, by
    the same brand rule the grid labels rows with. None (the default) counts
    the whole channel, unchanged from before this parameter existed.
    """
    source = db_models.get_table(conn.engine, "staging", f"{channel}_products")
    engine_where = _engine_clause(source, engine)

    def count(clause) -> int:
        return conn.execute(
            sa.select(sa.func.count()).select_from(source).where(clause, engine_where)
        ).scalar() or 0

    approved = count(_approved_clause(source))
    rejected = count(_rejected_clause(source))
    non_approved = count(_not_approved_clause(source))
    total = count(sa.true())

    return {
        "channel": channel,
        "engine": engine,
        "approved": approved,
        "rejected": rejected,
        "non_approved": non_approved,
        "pending": non_approved - rejected,
        "total": total,
    }


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
                # Which engine owns this row, resolved here rather than by the
                # client re-deriving the brand rule. A "reset and run" that
                # guessed would either miss the row or launch a second engine
                # that finds nothing -- the latter is what happened: every
                # single-SKU reset spawned a competitor run with to_run=0.
                "engine": (
                    "himalaya"
                    if (r["brand"] or "").strip().lower() in _himalaya_brands()
                    else "competitor"
                ),
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


_STATUS_FILTERS = {
    "approved": _approved_clause,
    "rejected": _rejected_clause,
    "non_approved": _not_approved_clause,
    # "pending" narrows non_approved to rows nobody has touched at all --
    # non_approved alone would still include rejected rows, which is the
    # wrong list to hand someone who asked for "everything still pending".
    "pending": lambda source: sa.and_(
        _not_approved_clause(source),
        sa.not_(_rejected_clause(source)),
    ),
    "all": lambda source: sa.true(),
}


def review_listing(
    conn,
    channel: str,
    *,
    status: str = "all",
    category: str | None = None,
    engine_filter=None,
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Every record on the channel, filterable by review status.

    The general-purpose grid behind the counts screen: "all" shows approved,
    rejected and pending side by side so an operator can run any individual
    SKU from one place, rather than needing to know in advance which of the
    three lists it's in. `status` narrows it the same way the count tiles do
    -- same clauses as counts() above, so a tile's number and the rows behind
    it when clicked can never disagree.

    Ordered by sku rather than reviewed_at: most rows here have never been
    reviewed at all, so reviewed_at is NULL for the majority and would sort
    them arbitrarily.
    """
    if status not in _STATUS_FILTERS:
        raise ValueError(f"status must be one of {tuple(_STATUS_FILTERS)}, got {status!r}")

    source = db_models.get_table(conn.engine, "staging", f"{channel}_products")
    mapping = db_models.get_table(conn.engine, "staging", f"{channel}_product_mapping")

    join = source.outerjoin(
        mapping,
        sa.and_(
            mapping.c[f"{channel}_product_id"] == source.c.id,
            mapping.c.match_rank == 1,
        ),
    )
    where = [_STATUS_FILTERS[status](source)]
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
            source.c.mapping_status, source.c.review_status,
            source.c.reviewed_by, source.c.reviewed_at, source.c.reviewer_comment,
            mapping.c.product_code, mapping.c.product_name,
            mapping.c.final_score, mapping.c.llm_reasoning,
        )
        .select_from(join).where(*where)
        .order_by(source.c.sku)
        .offset(offset).limit(limit)
    ).mappings().all()

    himalaya = _himalaya_brands()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "rows": [
            {
                "sku": r["sku"],
                "title": r["title"],
                "brand": r["brand"],
                "engine": (
                    "himalaya"
                    if (r["brand"] or "").strip().lower() in himalaya
                    else "competitor"
                ),
                "category": r["category"],
                "subcategory": r["subcategory"],
                "mapping_status": r["mapping_status"],
                # Normalised rather than passed through raw: NULL and '' both
                # mean "never reviewed" and the frontend needs one spelling.
                "review_status": (r["review_status"] or "").strip() or None,
                "matched_code": r["product_code"],
                "matched_name": r["product_name"],
                "score": float(r["final_score"]) if r["final_score"] is not None else None,
                "reasoning": r["llm_reasoning"],
                "reviewed_by": r["reviewed_by"],
                "reviewed_at": r["reviewed_at"].isoformat() if r["reviewed_at"] else None,
                "comment": r["reviewer_comment"],
            }
            for r in rows
        ],
    }


@dataclass(frozen=True)
class ResetRejectedResult:
    channel: str
    rows_found: int         # rejected rows matching the scope
    rows_reset: int         # flipped back to PENDING
    mappings_deleted: int   # mapping rows removed with them
    skus: list[str]         # exactly what was reset -- the re-run's scope
    # The same SKUs split by which engine owns them, so the re-run launches
    # one engine per leg instead of both. Launching both spawned a run that
    # found nothing on one side every time.
    by_engine: dict[str, list[str]]

    def as_dict(self) -> dict:
        return asdict(self)


def preview_reset(
    conn,
    channel: str,
    category: str | None = None,
    skus: list[str] | None = None,
    *,
    scope: str = "rejected",
    engine: str | None = None,
) -> ResetRejectedResult:
    """What reset_rejected would do. Reads only.

    Shares its selection with the apply below so the number shown to the
    operator and the number changed cannot drift apart -- the same reason
    recovery.py splits preview from apply.

    `skus` narrows it to named rows, which is how one rejection is reset on
    its own. Same code path as the channel-wide reset, just a narrower WHERE:
    a separate single-row function would be a second definition of "what a
    reset does", free to drift from this one.

    `scope` picks which rows are eligible at all -- "rejected" (the default,
    everything every existing caller already gets) or "non_approved" (also
    includes never-reviewed rows, for the review screen's bulk reset). Either
    way a steward-approved row can never be selected -- and NOT_APPROVED_GUARD
    below excludes one a second time, unconditionally, regardless of which
    scope is passed. Belt and suspenders on purpose: this function is the
    only thing standing between a UI bug and deleting a crosswalk-backed
    mapping, so "the scope clause happens to also exclude it" is not enough
    -- an approved row must be structurally impossible to select here even
    if _SCOPE_CLAUSES grows a new, careless scope later.

    `engine` narrows it further to "himalaya" or "competitor" rows by brand,
    same rule as counts()/review_listing(). This is what makes a true
    channel-wide "reset every Non-Approved Himalaya SKU" possible without
    naming a single sku.
    """
    source = db_models.get_table(conn.engine, "staging", f"{channel}_products")
    where = [
        _scope_clause(scope)(source),
        _engine_clause(source, engine),
        NOT_APPROVED_GUARD(source),
    ]
    if category:
        where.append(source.c.category == category)
    if skus:
        where.append(source.c.sku.in_(skus))

    # Brand comes back with the sku so the caller can launch one engine per
    # leg. Deciding that here rather than in the client keeps the brand rule
    # in one place.
    rows = conn.execute(
        sa.select(source.c.sku, source.c.brand).where(*where).order_by(source.c.sku)
    ).all()

    himalaya = _himalaya_brands()
    by_engine: dict[str, list[str]] = {"himalaya": [], "competitor": []}
    for sku, brand in rows:
        side = "himalaya" if (brand or "").strip().lower() in himalaya else "competitor"
        by_engine[side].append(sku)

    return ResetRejectedResult(
        channel=channel, rows_found=len(rows), rows_reset=0,
        mappings_deleted=0, skus=[s for s, _ in rows], by_engine=by_engine,
    )


def reset_rejected(
    conn,
    channel: str,
    *,
    category: str | None = None,
    skus: list[str] | None = None,
    actor: str,
    scope: str = "rejected",
    engine: str | None = None,
) -> ResetRejectedResult:
    """Clear the rejected matches and queue their SKUs for a fresh run.

    Four changes, one transaction:

      * the mapping rows go, because they ARE the rejected match -- leaving
        them means the listing still shows the match a human turned down,
      * mapping_status returns to PENDING, because step 6 selects only
        PENDING and anything else is skipped in silence,
      * match_rank and mapping_id are cleared, because they point at the rows
        being deleted and would leave a queued row looking mapped,
      * review_status is cleared, because the verdict was about a match that
        no longer exists.

    Returns the SKU list, which is the point: once review_status is cleared
    these rows are no longer identifiable as previously-rejected, so the
    caller has to carry the list into the re-run. Nothing else can recover it.

    Steward-approved rows are never touched regardless of `scope` -- neither
    _rejected_clause nor _not_approved_clause can match one, and that is not
    a flag a caller can pass. `skus` is honoured even for a sku that isn't
    currently rejected/non-approved (e.g. a caller resetting an arbitrary,
    manually-entered SKU) as long as it isn't approved: the scope clause is
    what keeps an approved row untouched, not a requirement that the row
    already look reset-worthy.
    """
    source = db_models.get_table(conn.engine, "staging", f"{channel}_products")
    mapping = db_models.get_table(conn.engine, "staging", f"{channel}_product_mapping")

    before = preview_reset(conn, channel, category, skus, scope=scope, engine=engine)
    if before.rows_found == 0:
        return before

    where = [
        _scope_clause(scope)(source),
        _engine_clause(source, engine),
        NOT_APPROVED_GUARD(source),
    ]
    if category:
        where.append(source.c.category == category)
    if skus:
        where.append(source.c.sku.in_(skus))

    ids = conn.execute(sa.select(source.c.id).where(*where)).scalars().all()

    # Second, independent check on the actual rows about to be mutated --
    # not just "the WHERE clause should have excluded them" but "assert none
    # of the ids selected are approved" against the live table state in this
    # same transaction. A mismatch here means the guard above has a bug, and
    # this function refuses to touch anything rather than delete on faith.
    if ids:
        approved_among_selected = conn.execute(
            sa.select(sa.func.count())
            .select_from(source)
            .where(source.c.id.in_(ids), _approved_clause(source))
        ).scalar() or 0
        if approved_among_selected:
            raise AssertionError(
                f"reset_rejected selected {approved_among_selected} steward-approved "
                f"row(s) on {channel} -- refusing to reset anything. This means the "
                f"scope/engine guard clauses disagree with the table; report this "
                f"before retrying."
            )

    # Chunked: SQL Server caps a statement at 2,100 parameters and IN (...)
    # spends one per value, so a large rejection backlog would otherwise fail
    # with a driver error naming neither the limit nor the cause.
    CHUNK = 1000
    deleted = 0
    for i in range(0, len(ids), CHUNK):
        batch = ids[i:i + CHUNK]
        deleted += conn.execute(
            sa.delete(mapping).where(mapping.c[f"{channel}_product_id"].in_(batch))
        ).rowcount or 0

    values = {"mapping_status": "PENDING", "review_status": None,
              "reviewed_by": None, "reviewed_at": None, "reviewer_comment": None}
    for optional in ("match_rank", "mapping_id"):
        if hasattr(source.c, optional):
            values[optional] = None

    reset = 0
    for i in range(0, len(ids), CHUNK):
        reset += conn.execute(
            sa.update(source)
            .where(source.c.id.in_(ids[i:i + CHUNK]))
            .values(**values)
        ).rowcount or 0

    # audit.activity_log carries CHECK (isjson(input) = 1), so the payload is
    # a JSON document rather than prose.
    activity = db_models.get_table(conn.engine, "audit", "activity_log")
    conn.execute(
        sa.insert(activity).values(
            entity=f"staging.{channel}_products",
            entity_key=(
                f"{channel}/{skus[0]}" if skus and len(skus) == 1
                else f"{channel}/{category or 'all'}"
            )[:1000],
            action="REJECTED_ROWS_RESET" if scope == "rejected" else "NON_APPROVED_ROWS_RESET",
            input=json.dumps({
                "channel": channel,
                "scope": scope,
                "engine": engine,
                "category": category,
                # Named so the trail distinguishes "reset this one row" from
                # "reset everything on the channel" -- they read identically
                # otherwise, and only one of them is a bulk action.
                "explicit_skus": len(skus) if skus else None,
                "rows_found": before.rows_found,
                "rows_reset": reset,
                "mappings_deleted": deleted,
                "sample_skus": before.skus[:10],
            }),
            logged_by=actor,
        )
    )

    # One transaction: the mappings go, the rows re-queue, and the trail
    # saying who did it lands together -- or none of it does.
    conn.commit()

    return ResetRejectedResult(
        channel=channel, rows_found=before.rows_found, rows_reset=reset,
        mappings_deleted=deleted, skus=before.skus, by_engine=before.by_engine,
    )


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
