"""How deep does the judge actually reach into the candidate shortlist?

Answers "should TOP_N_OUTPUT be 3, 5 or 10?" WITHOUT needing ground-truth
labels, by measuring where the judge's chosen candidate sat in the ensemble's
own ordering.

If the judge picks the ensemble's #1 almost every time, a long shortlist is
wasted context. If it regularly reaches position 4-10, then a shortlist of 3
was structurally preventing those matches -- the right product was never on
the list to be chosen.

This is a retrieval-depth question, not a correctness question: it says what a
shorter list would have made IMPOSSIBLE, which is exactly the part of the
top-3-vs-10 decision that can be settled objectively.

    python -m eval.shortlist_depth --channel swiggy
"""
from __future__ import annotations

import argparse

from common import db

CHANNELS = ["amazon", "blinkit", "swiggy", "zepto"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="swiggy", choices=CHANNELS)
    parser.add_argument("--since", default="2026-09-08",
                        help="Only consider mapping rows written on/after this date")
    args = parser.parse_args()

    channel = args.channel
    id_col = f"{channel}_product_id"

    conn = db.get_connection()
    try:
        rows = conn.exec_driver_sql(
            f"""
            WITH ranked AS (
              SELECT m.{id_col} AS pid,
                     m.match_rank,
                     ROW_NUMBER() OVER (
                       PARTITION BY m.{id_col} ORDER BY m.ensemble_score DESC
                     ) AS ens_pos
              FROM staging.{channel}_product_mapping m
              WHERE m.created_at >= ?
            )
            SELECT pid, ens_pos FROM ranked WHERE match_rank = 1
            """,
            (args.since,),
        ).fetchall()

        if not rows:
            print(f"No mapping rows for {channel} since {args.since}.")
            return

        positions = [int(pos) for _, pos in rows]
        total = len(positions)

        print(f"\nChannel: {channel}   rows judged: {total}\n")
        print(f"{'Judge picked ensemble #':<26}{'rows':>7}{'share':>9}")
        print("-" * 42)
        for position in sorted(set(positions)):
            count = positions.count(position)
            print(f"{position:<26}{count:>7}{100.0 * count / total:>8.1f}%")

        print("-" * 42)
        print("\nWhat each shortlist depth would have allowed:\n")
        for depth in (1, 3, 5, 10):
            reachable = sum(1 for p in positions if p <= depth)
            lost = total - reachable
            print(
                f"  TOP_N_OUTPUT={depth:<3} reachable={reachable:>5}/{total} "
                f"({100.0 * reachable / total:5.1f}%)   "
                f"structurally lost={lost}"
            )
        print()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
