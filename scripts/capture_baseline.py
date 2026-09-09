"""Capture the OLD engine's answers into audit.mapping_baseline.

Dry run by default -- reports what it would capture and writes nothing:

    python -m scripts.capture_baseline --label pre-v3-engine

Write it:

    python -m scripts.capture_baseline --label pre-v3-engine --apply

WHY THIS EXISTS

The new engine overwrites each SKU's mapping row in place (that is what
persist_sku_disposition does), so once it runs the OLD answer is gone. Without
a snapshot there is nothing to compare against and no way to say whether the
change helped.

WHAT IS FROZEN, AND WHY IT MATTERS

review_status is copied INTO the snapshot rather than read at comparison time.
Stewards keep working, so reading it later would score the new engine against
a moving target -- a row approved tomorrow would look like the new engine got
it right today. Freezing the verdict as at capture time is what makes TP/FP
countable.

All three ranks are captured, not only the winner. The interesting failure is
often not "the engine picked the wrong product" but "the engine had the right
product at rank 2" -- measured on live data, the correct row for a Tan Removal
listing sat below a turmeric face pack. Only the full list separates those, and
it is also what shows whether a new engine promoted a candidate the old one
already had or retrieved something it never saw.
"""
from __future__ import annotations

import argparse
import sys

import sqlalchemy as sa

from common import db

CHANNELS = ("amazon", "blinkit", "swiggy", "zepto")


def _select_sql(channel: str) -> str:
    """Every ranked candidate + frozen review state for one channel.

    The LEFT JOIN is what keeps a SKU with no candidates in the snapshot at
    all -- it appears once with match_rank coalesced to 1 and every old_*
    column null. Dropping those rows would make "retrieval found nothing" and
    "this SKU was never snapshotted" indistinguishable later.

    The row_number() de-duplicates defensively. A (sku, rank) pair should now
    be unique, but this table did carry duplicates from a delete that was
    scoped to batch_id, and a snapshot that silently doubled a channel would
    be worse than no snapshot at all.
    """
    return f"""
    select
        '{channel}'                       as channel,
        p.sku,
        coalesce(m.match_rank, 1)         as match_rank,
        p.title,
        p.category,
        p.subcategory,
        cast(p.pack_size as varchar(50))  as pack_size,
        cast(p.uom as varchar(50))        as uom,
        m.mapping_id,
        m.product_code,
        m.product_name,
        m.ensemble_score,
        m.final_score,
        m.confidence_level,
        p.mapping_status,
        m.relationship_type,
        m.llm_reasoning,
        m.batch_id,
        m.lexical_score,
        m.vector_score,
        m.pack_score,
        m.category_score,
        p.review_status,
        p.reviewed_by,
        p.reviewed_at
    from staging.{channel}_products p
    left join (
        select *, row_number() over (partition by source_sku, match_rank
                                     order by created_at desc, mapping_id desc) rn
        from   staging.{channel}_product_mapping
    ) m on m.source_sku = p.sku and m.rn = 1
    where upper(ltrim(rtrim(p.brand))) = 'HIMALAYA'
    """


def main() -> int:
    parser = argparse.ArgumentParser(description="Snapshot the OLD engine's answers.")
    parser.add_argument("--label", required=True,
                        help="Snapshot name, e.g. 'pre-v3-engine'.")
    parser.add_argument("--apply", action="store_true",
                        help="Write. Without this the script only reports.")
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        try:
            existing = conn.execute(
                sa.text("select count(*) from audit.mapping_baseline "
                        "where snapshot_label = :l"),
                {"l": args.label},
            ).scalar()
        except Exception:
            print("audit.mapping_baseline does not exist.")
            print("Apply sql/013_baseline_snapshot.sql first.")
            return 1

        if existing:
            print(f"Snapshot '{args.label}' already holds {existing} rows.")
            print("Choose a different --label; an existing snapshot is never overwritten.")
            return 1

        rows = []
        for channel in CHANNELS:
            rows.extend(conn.execute(sa.text(_select_sql(channel))).mappings().all())

        total = len(rows)
        by_rank: dict[int, int] = {}
        by_review: dict[str, int] = {}
        by_status: dict[str, int] = {}
        skus: set[tuple[str, str]] = set()
        no_candidates = 0
        for row in rows:
            by_rank[row["match_rank"]] = by_rank.get(row["match_rank"], 0) + 1
            skus.add((row["channel"], row["sku"]))
            if row["mapping_id"] is None:
                no_candidates += 1
            # Count review/status once per SKU, not once per candidate --
            # otherwise a 3-candidate SKU would be counted three times and the
            # totals would not reconcile with the 3692 in staging.
            if row["match_rank"] == 1:
                key = row["review_status"] or "NULL"
                by_review[key] = by_review.get(key, 0) + 1
                skey = row["mapping_status"] or "NULL"
                by_status[skey] = by_status.get(skey, 0) + 1

        print()
        print("=" * 60)
        print(f"BASELINE '{args.label}'" + ("" if args.apply else "   (dry run)"))
        print("=" * 60)
        print(f"  candidate rows to capture : {total}")
        print(f"  distinct SKUs             : {len(skus)}")
        print(f"  SKUs with no candidates   : {no_candidates}")
        print()
        print("  by rank:")
        for rank in sorted(by_rank):
            print(f"     rank {rank}                  {by_rank[rank]}")
        print()
        print("  frozen review_status (per SKU):")
        for key in sorted(by_review):
            print(f"     {key:<24}{by_review[key]}")
        print()
        print("  frozen mapping_status (per SKU):")
        for key in sorted(by_status, key=lambda k: -by_status[k]):
            print(f"     {key:<24}{by_status[key]}")

        if not args.apply:
            print()
            print("  Nothing written. Re-run with --apply to save.")
            return 0

        insert = sa.text("""
            insert into audit.mapping_baseline (
                snapshot_label, channel, sku, match_rank, title, category, subcategory,
                pack_size, uom, old_mapping_id, old_product_code, old_product_name,
                old_ensemble_score, old_final_score, old_confidence,
                old_mapping_status, old_relationship, old_llm_reasoning, old_batch_id,
                old_lexical_score, old_vector_score, old_pack_score, old_category_score,
                old_review_status, old_reviewed_by, old_reviewed_at)
            values (
                :label, :channel, :sku, :match_rank, :title, :category, :subcategory,
                :pack_size, :uom, :mapping_id, :product_code, :product_name,
                :ensemble_score, :final_score, :confidence_level,
                :mapping_status, :relationship_type, :llm_reasoning, :batch_id,
                :lexical_score, :vector_score, :pack_score, :category_score,
                :review_status, :reviewed_by, :reviewed_at)
        """)

        try:
            for row in rows:
                conn.execute(insert, {"label": args.label, **dict(row)})
            written = conn.execute(
                sa.text("select count(*) from audit.mapping_baseline "
                        "where snapshot_label = :l"),
                {"l": args.label},
            ).scalar()
            if written != total:
                conn.rollback()
                print(f"\n  ROLLED BACK -- wrote {written}, expected {total}")
                return 1
            conn.commit()
        except Exception as exc:
            conn.rollback()
            print(f"\n  ROLLED BACK: {str(exc)[:200]}")
            return 1

        print()
        print(f"  COMMITTED: {written} rows captured under '{args.label}'.")
        print()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
