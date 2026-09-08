"""Himalaya-brand mapping quality snapshot.

Run before and after an engine change to get a comparable set of numbers:

    python -m eval.mapping_stats --label baseline
    python -m eval.mapping_stats --label after-fixes
    python -m eval.mapping_stats --compare baseline after-fixes

Each run writes eval/snapshots/<label>.json and prints a table. Scope is
Himalaya-brand source rows only (brand LIKE '%himalaya%') across the four
channel tables -- competitor rows are deliberately excluded, they are a
different mapping problem with a different success criterion.

Nothing here writes to the mapping tables; it only reads.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

from common import db

CHANNELS = ["amazon", "blinkit", "swiggy", "zepto"]

# The order results are reported in. AutoMatch first (the outcome that
# actually ships), NoHimalayaEquivalent last.
STATUS_ORDER = [
    "AutoMatch",
    "StewardReview",
    "LowConfidence",
    "NoHimalayaEquivalent",
    "Deterministic",
    "Failed",
    "PENDING",
]

# Himalaya-brand filter, applied identically everywhere so the before/after
# populations are the same rows.
BRAND_FILTER = "LOWER(brand) LIKE '%himalaya%'"

SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "snapshots")


def _status_counts(conn, channel: str) -> dict[str, int]:
    rows = conn.exec_driver_sql(
        f"""
        SELECT mapping_status, COUNT(*)
        FROM staging.{channel}_products
        WHERE {BRAND_FILTER}
        GROUP BY mapping_status
        """
    ).fetchall()
    return {str(status): int(count) for status, count in rows}


def _score_stats(conn, channel: str) -> dict[str, float | int | None]:
    """Rank-1 ensemble/final score distribution for Himalaya rows.

    Joined back to the product table so the brand filter applies -- the
    mapping table itself carries no brand column.
    """
    row = conn.exec_driver_sql(
        f"""
        SELECT
            COUNT(*),
            AVG(CAST(m.ensemble_score AS FLOAT)),
            MIN(CAST(m.ensemble_score AS FLOAT)),
            MAX(CAST(m.ensemble_score AS FLOAT)),
            AVG(CAST(m.final_score AS FLOAT))
        FROM staging.{channel}_product_mapping m
        JOIN staging.{channel}_products p
          ON p.id = m.{channel}_product_id
        WHERE m.match_rank = 1 AND {BRAND_FILTER.replace('brand', 'p.brand')}
        """
    ).fetchone()

    if not row or not row[0]:
        return {"rank1_rows": 0, "avg_ensemble": None,
                "min_ensemble": None, "max_ensemble": None, "avg_final": None}

    return {
        "rank1_rows": int(row[0]),
        "avg_ensemble": round(float(row[1]), 4) if row[1] is not None else None,
        "min_ensemble": round(float(row[2]), 4) if row[2] is not None else None,
        "max_ensemble": round(float(row[3]), 4) if row[3] is not None else None,
        "avg_final": round(float(row[4]), 4) if row[4] is not None else None,
    }


def collect(conn) -> dict:
    per_channel: dict[str, dict] = {}
    totals: dict[str, int] = {}

    for channel in CHANNELS:
        counts = _status_counts(conn, channel)
        per_channel[channel] = {
            "status_counts": counts,
            "total": sum(counts.values()),
            "scores": _score_stats(conn, channel),
        }
        for status, count in counts.items():
            totals[status] = totals.get(status, 0) + count

    grand_total = sum(totals.values())
    processed = grand_total - totals.get("PENDING", 0)
    auto = totals.get("AutoMatch", 0)

    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "scope": "himalaya-brand source rows only",
        "per_channel": per_channel,
        "totals": totals,
        "grand_total": grand_total,
        "processed": processed,
        # The headline number: of rows the engine actually decided, how many
        # did it decide with enough confidence to ship without a human?
        "automatch_rate_of_processed": (
            round(100.0 * auto / processed, 2) if processed else None
        ),
        "needs_human_or_failed": (
            totals.get("StewardReview", 0) + totals.get("LowConfidence", 0)
        ),
    }


def _fmt_table(snapshot: dict) -> str:
    lines = []
    totals = snapshot["totals"]
    processed = snapshot["processed"]

    lines.append(f"Scope    : {snapshot['scope']}")
    lines.append(f"Captured : {snapshot['captured_at']}")
    lines.append("")
    lines.append(f"{'Status':<24}{'Count':>8}{'% of processed':>16}")
    lines.append("-" * 48)
    for status in STATUS_ORDER:
        if status not in totals:
            continue
        count = totals[status]
        if status == "PENDING":
            lines.append(f"{status:<24}{count:>8}{'(not processed)':>16}")
        else:
            pct = 100.0 * count / processed if processed else 0.0
            lines.append(f"{status:<24}{count:>8}{pct:>15.1f}%")
    lines.append("-" * 48)
    lines.append(f"{'TOTAL (himalaya)':<24}{snapshot['grand_total']:>8}")
    lines.append(f"{'processed':<24}{processed:>8}")
    lines.append("")
    lines.append(f"AutoMatch rate (of processed): {snapshot['automatch_rate_of_processed']}%")
    lines.append(f"Rows needing a human         : {snapshot['needs_human_or_failed']}")
    lines.append("")
    lines.append("Per channel:")
    for channel, data in snapshot["per_channel"].items():
        scores = data["scores"]
        lines.append(
            f"  {channel:<9} total={data['total']:>6}  "
            f"rank1_rows={scores['rank1_rows']:>6}  "
            f"avg_ensemble={scores['avg_ensemble']}"
        )
    return "\n".join(lines)


def _delta(before: dict, after: dict) -> str:
    lines = []
    lines.append(f"{'Status':<24}{'Before':>9}{'After':>9}{'Delta':>9}")
    lines.append("-" * 51)

    statuses = [s for s in STATUS_ORDER
                if s in before["totals"] or s in after["totals"]]
    for status in statuses:
        b = before["totals"].get(status, 0)
        a = after["totals"].get(status, 0)
        lines.append(f"{status:<24}{b:>9}{a:>9}{a - b:>+9}")

    lines.append("-" * 51)
    b_rate = before["automatch_rate_of_processed"] or 0.0
    a_rate = after["automatch_rate_of_processed"] or 0.0
    lines.append(
        f"{'AutoMatch rate %':<24}{b_rate:>9.2f}{a_rate:>9.2f}{a_rate - b_rate:>+9.2f}"
    )
    b_human = before["needs_human_or_failed"]
    a_human = after["needs_human_or_failed"]
    lines.append(
        f"{'Needs a human':<24}{b_human:>9}{a_human:>9}{a_human - b_human:>+9}"
    )
    return "\n".join(lines)


def _load(label: str) -> dict:
    path = os.path.join(SNAPSHOT_DIR, f"{label}.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", help="Capture a snapshot under this name")
    parser.add_argument(
        "--compare", nargs=2, metavar=("BEFORE", "AFTER"),
        help="Print the delta between two previously captured snapshots",
    )
    args = parser.parse_args()

    if args.compare:
        before, after = (_load(name) for name in args.compare)
        print(f"\n=== {args.compare[0]}  ->  {args.compare[1]} ===\n")
        print(_delta(before, after))
        print()
        return

    if not args.label:
        parser.error("pass --label to capture, or --compare to diff")

    conn = db.get_connection()
    try:
        snapshot = collect(conn)
    finally:
        conn.close()

    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    path = os.path.join(SNAPSHOT_DIR, f"{args.label}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, indent=2)

    print()
    print(_fmt_table(snapshot))
    print()
    print(f"Snapshot written to {path}")


if __name__ == "__main__":
    main()
