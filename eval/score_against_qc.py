"""Score whatever is currently in the database against the QC ground truth.

    python -m eval.consolidate_qc --qc-dir "D:/Himalaya/QC_data"   # build truth
    python -m eval.score_against_qc                                # measure

READ ONLY. No writes, no LLM, no cost -- run it as often as you like, before
and after a change, to see whether the change actually helped.

Scores only the SKUs that HAVE a rank-1 mapping row. A SKU still PENDING has
not been answered yet, and counting it as a miss would report the engine as
wrong for work it has not done. The header prints that coverage, because an
accuracy figure means nothing without it.

Three numbers are reported, and they answer different questions:

  top-1   the engine's rank 1 is the reviewer's answer. This is the number
          that matters -- it is what ships to a steward as the default.
  top-3   the answer is somewhere in ranks 1-3. The gap between top-1 and
          top-3 is the ceiling a better RANKING could reach without better
          RETRIEVAL; if they are close, ranking is not the bottleneck.
  no-eq   for the rows where the reviewer found NOTHING worth mapping to, did
          the engine also decline? Credited as correct, never as a miss --
          an engine that correctly says "no equivalent" is right, and scoring
          it as wrong would push the engine toward inventing matches.
"""
from __future__ import annotations

import argparse
import os

import pandas as pd
import sqlalchemy as sa

from common import db, db_models

CHANNELS = ("amazon", "zepto", "blinkit", "swiggy")

# Statuses that mean "the engine declined to map this row".
DECLINED = {"nohimalayaequivalent", "no himalaya equivalent"}


def _norm(value) -> str | None:
    """Product codes to a canonical string -- see consolidate_qc._clean_code."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none"):
        return None
    if text.endswith(".0"):
        text = text[:-2]
    return text


def fetch_engine_picks(conn, channel: str, skus: list[str]) -> dict[str, dict]:
    """{sku: {rank: product_code, ..., 'status': mapping_status}} for one channel."""
    tables = db_models.resolve_channel_tables(conn.engine, channel)
    mapping, products = tables.mapping, tables.products
    picks: dict[str, dict] = {}

    # SQL Server caps bound parameters at 2100; chunk well under it.
    for start in range(0, len(skus), 500):
        chunk = skus[start:start + 500]

        rows = conn.execute(
            sa.select(
                mapping.c.source_sku, mapping.c.match_rank, mapping.c.product_code
            ).where(
                sa.and_(mapping.c.source_sku.in_(chunk), mapping.c.match_rank <= 3)
            )
        ).all()
        for sku, rank, code in rows:
            entry = picks.setdefault(str(sku), {})
            # Duplicate rank rows would otherwise overwrite silently; keep the
            # first and let the count of them surface in the report.
            entry.setdefault(int(rank), _norm(code))

        for sku, status in conn.execute(
            sa.select(products.c.sku, products.c.mapping_status)
            .where(products.c.sku.in_(chunk))
        ).all():
            picks.setdefault(str(sku), {}).setdefault("status", str(status or ""))

    return picks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--truth", default="eval/qc_ground_truth.csv")
    parser.add_argument("--category", default=None,
                        help="limit to one 1DS category (e.g. 'face care')")
    parser.add_argument("--out", default=None,
                        help="write the per-row detail to this CSV")
    args = parser.parse_args()

    if not os.path.exists(args.truth):
        raise SystemExit(
            f"{args.truth} not found -- run eval.consolidate_qc first."
        )

    truth = pd.read_csv(args.truth, dtype=str)
    if args.category:
        truth = truth[
            truth["oneds_category"].str.strip().str.lower()
            == args.category.strip().lower()
        ]
        if truth.empty:
            raise SystemExit(f"no QC rows for category {args.category!r}")

    conn = db.get_connection()
    try:
        records = []
        for channel in CHANNELS:
            subset = truth[truth["channel"] == channel]
            if subset.empty:
                continue
            picks = fetch_engine_picks(
                conn, channel, subset["sku"].unique().tolist()
            )
            for _, row in subset.iterrows():
                got = picks.get(row["sku"], {})
                expected = _norm(row["gt_product_code"])
                ranks = [got.get(1), got.get(2), got.get(3)]
                status = str(got.get("status", "")).strip().lower()
                records.append(
                    {
                        "channel": channel,
                        "sku": row["sku"],
                        "category": row.get("oneds_category", ""),
                        "subcategory": row.get("oneds_subcategory", ""),
                        "title": row.get("title", ""),
                        "expected": expected,
                        "expected_is_none": expected is None,
                        "engine_r1": ranks[0],
                        "engine_r2": ranks[1],
                        "engine_r3": ranks[2],
                        "status": status,
                        "scored": ranks[0] is not None,
                        "top1": expected is not None and ranks[0] == expected,
                        "top3": expected is not None and expected in ranks,
                        "declined": status in DECLINED,
                    }
                )
    finally:
        conn.close()

    frame = pd.DataFrame(records)
    scored = frame[frame["scored"]]
    mappable = scored[~scored["expected_is_none"]]
    no_eq = scored[scored["expected_is_none"]]

    print("=" * 74)
    print("ENGINE vs QC GROUND TRUTH")
    print("=" * 74)
    print(f"QC rows loaded          : {len(frame)}")
    print(f"  answered by engine    : {len(scored)}  <- only these are scored")
    print(f"  not yet run (PENDING) : {len(frame) - len(scored)}")
    if not len(scored):
        raise SystemExit("\nnothing to score yet -- no rank-1 rows for these SKUs.")

    print(f"\nOn the {len(mappable)} rows the reviewer gave a product_code:")
    top1 = mappable["top1"].sum()
    top3 = mappable["top3"].sum()
    print(f"  top-1 correct         : {top1:>5} / {len(mappable)}  "
          f"({100 * top1 / len(mappable):.1f}%)")
    print(f"  top-3 correct         : {top3:>5} / {len(mappable)}  "
          f"({100 * top3 / len(mappable):.1f}%)")
    print(f"  in ranks 2-3 only     : {top3 - top1:>5}  "
          f"<- reachable by better ranking alone")

    if len(no_eq):
        declined = no_eq["declined"].sum()
        print(f"\nOn the {len(no_eq)} rows the reviewer found NO valid mapping:")
        print(f"  engine also declined  : {declined:>5} / {len(no_eq)}  "
              f"({100 * declined / len(no_eq):.1f}%)")

    print(f"\n{'CATEGORY':<34}{'n':>6}{'top-1':>9}{'top-3':>9}")
    print("-" * 74)
    grouped = mappable.groupby("category")
    for name, group in sorted(grouped, key=lambda kv: -len(kv[1])):
        share1 = 100 * group["top1"].sum() / len(group)
        share3 = 100 * group["top3"].sum() / len(group)
        print(f"{str(name)[:32]:<34}{len(group):>6}{share1:>8.1f}%{share3:>8.1f}%")

    print(f"\n{'CHANNEL':<34}{'n':>6}{'top-1':>9}{'top-3':>9}")
    print("-" * 74)
    for name, group in mappable.groupby("channel"):
        share1 = 100 * group["top1"].sum() / len(group)
        share3 = 100 * group["top3"].sum() / len(group)
        print(f"{name:<34}{len(group):>6}{share1:>8.1f}%{share3:>8.1f}%")

    if args.out:
        frame.to_csv(args.out, index=False, encoding="utf-8-sig")
        print(f"\nper-row detail written: {args.out}")


if __name__ == "__main__":
    main()
