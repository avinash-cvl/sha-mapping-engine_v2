"""Rebuild config.calibration_buckets from the steward verdicts in staging.

Dry run by default -- prints what it would write and changes nothing:

    python -m scripts.build_calibration

Write it, recording which scoring run the counts came from:

    python -m scripts.build_calibration --apply --note "2026-09-08 baseline"

WHY THE NOTE MATTERS

A verdict is evidence about the candidate the steward was actually shown, so
it only describes the scores from that run. Re-run the engine, and a row whose
rank-1 changed is now paired with a different candidate -- the old verdict no
longer says anything about the new pairing. Mixing runs is the main way a
calibration table goes quietly wrong, and --note is the only record of which
run a bucket's counts came from.

The script refuses to arm any bucket that has fewer than --min-labels behind
it; those keep falling back to the existing max()/floor logic.
"""
from __future__ import annotations

import argparse
import sys

from common import db
from oneds_master.stages import stage_calibration as calib


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild the TP/FP calibration table from steward verdicts.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the results. Without this the script only reports.",
    )
    parser.add_argument(
        "--min-labels",
        type=int,
        default=30,
        help="Labels required before a bucket is armed (default: 30). "
             "About +/- 9 points at 95%% confidence -- the coarsest estimate "
             "still worth acting on.",
    )
    parser.add_argument(
        "--note",
        default=None,
        help="Which scoring run these counts came from. Strongly recommended.",
    )
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        results = calib.rebuild(
            conn,
            min_labels=args.min_labels,
            note=args.note,
            dry_run=not args.apply,
        )

        if not results:
            print("No labelled rows found. Nothing to calibrate yet.")
            print("A row counts only when it has BOTH a review_status of")
            print("Approved/Rejected AND a rank-1 mapping row with scores.")
            return 0

        print()
        print("=" * 96)
        print("CALIBRATION" + ("" if args.apply else "  (dry run -- nothing written)"))
        print("=" * 96)
        header = (
            f"  {'bucket':<12}{'labels':>8}{'appr':>7}{'rej':>6}"
            f"{'precision':>11}{'95% low':>10}  {'status'}"
        )
        print(header)
        print("  " + "-" * 92)

        for row in results:
            flag = "ACTIVE" if row["is_active"] else row["reason"]
            print(
                f"  {row['bucket']:<12}{row['n_labelled']:>8}{row['n_approved']:>7}"
                f"{row['n_rejected']:>6}{row['precision']:>11.3f}"
                f"{row['precision_lower_95']:>10.3f}  {flag}"
            )

        total = sum(int(r["n_labelled"]) for r in results)
        active = [r for r in results if r["is_active"]]
        print("  " + "-" * 92)
        print(f"  {total} labelled rows across {len(results)} bucket(s); "
              f"{len(active)} armed")

        if active:
            print()
            print("  What the armed buckets would do:")
            for row in sorted(active, key=lambda r: -float(r["precision_lower_95"])):
                lower = float(row["precision_lower_95"])
                # Read off the 95% LOWER bound, never the raw rate: 3/3 is
                # 100% and means nothing, 280/300 is 93% and means a lot.
                if lower >= 0.86:
                    action = "AutoMatch"
                elif lower >= 0.61:
                    action = "StewardReview"
                else:
                    action = "LowConfidence"
                print(
                    f"    {row['bucket']:<12} -> {action:<16}"
                    f"(rate {float(row['precision']):.0%}, "
                    f"lower bound {lower:.0%}, n={row['n_labelled']})"
                )

        stale = [r for r in results if not r["is_active"]]
        if stale:
            print()
            print("  Falling back to existing logic (too few labels):")
            for row in stale:
                print(f"    {row['bucket']:<12} {row['reason']}")

        if not args.apply:
            print()
            print("  Nothing was written. Re-run with --apply --note \"<run>\" to save.")
        elif not args.note:
            print()
            print("  WARNING: written without --note. There is now no record of")
            print("  which scoring run these counts describe.")

        print()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
