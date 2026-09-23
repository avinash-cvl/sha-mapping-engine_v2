"""Rejections: the write path, the guards, and the reason it exists.

These run against the live staging database, so every test picks its own row,
records what it was, and restores it in a finally -- review_status is a real
column the steward portal reads, and a test must never leave a verdict behind
that nobody made.
"""
from __future__ import annotations

import sqlalchemy as sa
import pytest

from common import db, db_models, rejection

CHANNEL = "zepto"
ACTOR = "pytest-rejection@local.invalid"


@pytest.fixture
def conn():
    c = db.get_connection()
    yield c
    c.close()


@pytest.fixture
def decidable_sku(conn):
    """A listing with a rank-1 match and no steward verdict on it.

    Restored to exactly what it was, whatever the test did -- including the
    case where the test itself leaves it rejected.
    """
    source = db_models.get_table(conn.engine, "staging", f"{CHANNEL}_products")
    mapping = db_models.get_table(conn.engine, "staging", f"{CHANNEL}_product_mapping")

    row = conn.execute(
        sa.select(source.c.id, source.c.sku)
        .select_from(source.join(
            mapping,
            sa.and_(mapping.c[f"{CHANNEL}_product_id"] == source.c.id,
                    mapping.c.match_rank == 1),
        ))
        .where(source.c.review_status.is_(None))
        .limit(1)
    ).first()
    if row is None:
        pytest.skip(f"no unreviewed {CHANNEL} listing with a rank-1 match")

    yield row.sku

    conn.execute(
        sa.update(source).where(source.c.id == row.id).values(
            review_status=None, reviewed_by=None,
            reviewed_at=None, reviewer_comment=None,
        )
    )
    conn.commit()


def _review_status(conn, sku):
    source = db_models.get_table(conn.engine, "staging", f"{CHANNEL}_products")
    return conn.execute(
        sa.select(source.c.review_status).where(source.c.sku == sku)
    ).scalar()


def test_reject_records_the_proposed_code(conn, decidable_sku):
    """The rejection names what the engine actually proposed.

    Read from the mapping row rather than taken from the caller: a code
    supplied by a client is a claim about the past that nothing verifies.
    """
    result = rejection.reject(conn, CHANNEL, decidable_sku, actor=ACTOR,
                              comment="wrong variant")

    mapping = db_models.get_table(conn.engine, "staging", f"{CHANNEL}_product_mapping")
    source = db_models.get_table(conn.engine, "staging", f"{CHANNEL}_products")
    expected = conn.execute(
        sa.select(mapping.c.product_code)
        .select_from(source.join(
            mapping, mapping.c[f"{CHANNEL}_product_id"] == source.c.id))
        .where(source.c.sku == decidable_sku)
        .where(mapping.c.match_rank == 1)
    ).scalar()

    assert result.product_code == expected
    assert result.rejected_by == ACTOR
    assert _review_status(conn, decidable_sku) == rejection.REJECTED


def test_rejection_does_not_remove_the_sku_from_a_run(conn, decidable_sku):
    """A rejection is a verdict on the PAIRING, not on the listing.

    scope._not_approved_clause matches anything that is not 'Approved', so a
    rejected row stays eligible -- deliberately. Rejecting "this balm is not
    that balm" must not mean "never try to match this listing again".
    """
    from common import scope

    source = db_models.get_table(conn.engine, "staging", f"{CHANNEL}_products")
    before = conn.execute(
        sa.select(sa.func.count()).select_from(source)
        .where(source.c.sku == decidable_sku)
        .where(scope._not_approved_clause(source))
    ).scalar()

    rejection.reject(conn, CHANNEL, decidable_sku, actor=ACTOR)

    after = conn.execute(
        sa.select(sa.func.count()).select_from(source)
        .where(source.c.sku == decidable_sku)
        .where(scope._not_approved_clause(source))
    ).scalar()

    assert before == after == 1, "a rejected row must stay eligible for a re-run"


def test_scope_counts_rejections_separately(conn, decidable_sku):
    """The count is surfaced so the operator can see the engine is about to
    re-propose something a steward turned down."""
    from common import scope

    """Asked of BOTH engines rather than inferring which leg the row belongs
    to. The brand test is _brand_clause's membership of C.HIMALAYA_BRANDS, not
    a literal string compare -- inferring it here reimplemented that badly and
    the test watched the wrong leg's count."""
    before = sum(scope.resolve(conn, CHANNEL, e).rejected for e in scope.ENGINES)
    rejection.reject(conn, CHANNEL, decidable_sku, actor=ACTOR)
    after = sum(scope.resolve(conn, CHANNEL, e).rejected for e in scope.ENGINES)

    assert after == before + 1


def test_approved_rows_cannot_be_rejected_here(conn):
    """An approval has already written a crosswalk row the engine
    short-circuits on. Flipping the source row alone would leave the two
    disagreeing, with the crosswalk winning silently on the next run."""
    source = db_models.get_table(conn.engine, "staging", f"{CHANNEL}_products")
    sku = conn.execute(
        sa.select(source.c.sku)
        .where(sa.func.upper(sa.func.ltrim(sa.func.rtrim(source.c.review_status)))
               == "APPROVED")
        .limit(1)
    ).scalar()
    if sku is None:
        pytest.skip("no approved row on this channel")

    with pytest.raises(PermissionError):
        rejection.reject(conn, CHANNEL, sku, actor=ACTOR)

    assert _review_status(conn, sku) == "Approved", "the row must be untouched"


def test_unknown_sku_raises_rather_than_silently_doing_nothing(conn):
    with pytest.raises(LookupError):
        rejection.reject(conn, CHANNEL, "no-such-sku-000", actor=ACTOR)


def test_withdraw_returns_the_row_to_unreviewed(conn, decidable_sku):
    rejection.reject(conn, CHANNEL, decidable_sku, actor=ACTOR, comment="oops")
    assert rejection.withdraw(conn, CHANNEL, decidable_sku, actor=ACTOR) is True
    assert _review_status(conn, decidable_sku) is None


def test_withdraw_leaves_an_approval_alone(conn):
    """Only a rejection is withdrawable here -- clearing an approval would
    orphan its crosswalk row, which this module has no business doing."""
    source = db_models.get_table(conn.engine, "staging", f"{CHANNEL}_products")
    sku = conn.execute(
        sa.select(source.c.sku)
        .where(sa.func.upper(sa.func.ltrim(sa.func.rtrim(source.c.review_status)))
               == "APPROVED")
        .limit(1)
    ).scalar()
    if sku is None:
        pytest.skip("no approved row on this channel")

    assert rejection.withdraw(conn, CHANNEL, sku, actor=ACTOR) is False
    assert _review_status(conn, sku) == "Approved"


def test_listing_returns_the_rejected_pairing(conn, decidable_sku):
    rejection.reject(conn, CHANNEL, decidable_sku, actor=ACTOR, comment="why not")
    d = rejection.listing(conn, CHANNEL, limit=500)

    row = next((r for r in d["rows"] if r["sku"] == decidable_sku), None)
    assert row is not None, "a rejected row must appear in the listing"
    assert row["comment"] == "why not"
    assert row["reviewed_by"] == ACTOR


def test_offenders_counts_rejections_per_master_code(conn, decidable_sku):
    """The whole point of the feature: one product code across many
    rejections is the signature of a gate target too small to hold an
    alternative."""
    result = rejection.reject(conn, CHANNEL, decidable_sku, actor=ACTOR)
    if not result.product_code:
        pytest.skip("that row has no rank-1 product code")

    rows = rejection.offenders(conn, CHANNEL, limit=100)
    hit = next((r for r in rows if r["product_code"] == result.product_code), None)
    assert hit is not None
    assert hit["rejections"] >= 1


def test_rejected_clause_does_not_swallow_unreviewed_rows(conn):
    """review_status is NULL on every unreviewed row, and a NULL comparison
    is NULL rather than false -- the three-valued-logic trap that made an
    earlier count come back as zero for the entire table."""
    source = db_models.get_table(conn.engine, "staging", f"{CHANNEL}_products")

    rejected = conn.execute(
        sa.select(sa.func.count()).select_from(source)
        .where(rejection._rejected_clause(source))
    ).scalar()
    total = conn.execute(
        sa.select(sa.func.count()).select_from(source)
    ).scalar()

    assert rejected < total, "the clause must not match every row"
    assert rejected >= 0
