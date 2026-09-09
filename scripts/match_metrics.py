"""TP/FP/TN metrics for the mapping engine, measured against steward verdicts.

    python -m scripts.match_metrics --category "lip makeup"
    python -m scripts.match_metrics                      # whole book
    python -m scripts.match_metrics --baseline pre-v3-engine   # OLD vs NEW

WHY THIS EXISTS

Errors were being found one screenshot at a time. That does not scale to 180k
rows: the only rows anyone looks at are the ones that happen to be noticed, so
the error classes that get fixed are the ones that happen to be seen -- not the
ones that are common. This measures every labelled row at once, so work can be
prioritised by frequency instead of by whoever spotted something.

WHERE GROUND TRUTH COMES FROM

app.{channel}_product_crosswalk. When a steward approves a mapping the portal
writes the product_code they chose, so the crosswalk holds the RIGHT ANSWER,
not merely the fact that some answer was accepted. That makes TP/FP a direct
comparison of codes:

    TP  crosswalk has a code, engine picked the SAME code
    FP  crosswalk has a code, engine picked a DIFFERENT one
    FN  crosswalk has a code, engine is not confident enough to assert it

An approved row is never re-scored to measure this. It keeps the answer the
steward gave, and the comparison uses whatever the engine last produced for
that SKU. Re-running approved rows would spend an LLM call to re-derive a
settled decision, and briefly leave the row looking unprocessed.

Rejections are counted separately, from staging.*_products.review_status.
A rejection says the candidate shown was wrong; it does not say which product
was right, so it supports a false-positive rate but not precision -- there is
no correct code to compare against.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

import sqlalchemy as sa

from common import db

CHANNELS = ("amazon", "blinkit", "swiggy", "zepto")

# Tiers where the engine is asserting a match. Deterministic is included
# because it is the strongest assertion there is -- a mapping a human already
# approved -- not because the identity layer writes it (it does not).
CONFIDENT = {"AutoMatch", "DeterministicMatch", "Deterministic"}


def fetch(conn, category: str | None) -> list[dict]:
    rows: list[dict] = []
    for channel in CHANNELS:
        where = ["upper(ltrim(rtrim(p.brand))) = 'HIMALAYA'"]
        params: dict[str, object] = {}
        if category:
            where.append("p.category = :cat")
            params["cat"] = category
        sql = f"""
        select '{channel}' as channel, p.sku, p.title, p.category, p.subcategory,
               p.mapping_status, p.review_status,
               m.product_code, m.product_name, m.ensemble_score, m.llm_score,
               m.attribute_score, m.final_score_v2, m.match_method,
               m.identity_evidence, m.critical_conflict
        from staging.{channel}_products p
        left join (
            select *, row_number() over (partition by source_sku
                                         order by created_at desc, mapping_id desc) rn
            from   staging.{channel}_product_mapping
            where  match_rank = 1
        ) m on m.source_sku = p.sku and m.rn = 1
        where {' and '.join(where)}
        """
        rows.extend(dict(r) for r in conn.execute(sa.text(sql), params).mappings())
    return rows


def fetch_crosswalk(conn, category: str | None) -> dict[str, str]:
    """{channel|sku: product_code} -- the code a steward actually chose."""
    truth: dict[str, str] = {}
    for channel in CHANNELS:
        where = ["lower(ltrim(rtrim(x.match_status))) in ('approved','auto_approved')"]
        params: dict[str, object] = {}
        if category:
            where.append("p.category = :cat")
            params["cat"] = category
        sql = f"""
        select x.sku, x.product_code
        from   app.{channel}_product_crosswalk x
        join   staging.{channel}_products p on p.sku = x.sku
        where  {' and '.join(where)}
        """
        for row in conn.execute(sa.text(sql), params):
            if row.product_code:
                truth[f"{channel}|{row.sku}"] = str(row.product_code).strip()
    return truth


def confusion(rows: list[dict], truth: dict[str, str]) -> dict[str, int]:
    """TP/FP/FN against the steward's chosen code; TN from rejections.

    TP/FP compare CODES, not merely whether a match was asserted -- the
    crosswalk records which product is correct, so "the engine was confident"
    and "the engine was right" stay separable. An engine that confidently
    picks the wrong product is a false positive even though the SKU was
    approved, and the old approved-means-correct reading would have scored
    that as a success.
    """
    counts = {"TP": 0, "FP": 0, "TN": 0, "FN": 0}
    for row in rows:
        key = f"{row['channel']}|{row['sku']}"
        correct = truth.get(key)
        asserted = row["mapping_status"] in CONFIDENT
        picked = (row["product_code"] or "").strip()

        if correct is not None:
            if not asserted:
                counts["FN"] += 1          # right answer exists, engine withheld
            elif picked == correct:
                counts["TP"] += 1
            else:
                counts["FP"] += 1          # confident, and confidently wrong
            continue

        # No crosswalk entry. A rejection still tells us the engine should not
        # be asserting this pairing.
        if (row["review_status"] or "").strip().lower() == "rejected":
            counts["FP" if asserted else "TN"] += 1
    return counts


def rates(counts: dict[str, int]) -> dict[str, float | None]:
    tp, fp, fn = counts["TP"], counts["FP"], counts["FN"]
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    f1 = (2 * precision * recall / (precision + recall)
          if precision and recall and (precision + recall) else None)
    return {"precision": precision, "recall": recall, "f1": f1}


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure match quality against steward verdicts.")
    parser.add_argument("--category", default=None, help="Limit to one 1DS category.")
    parser.add_argument("--baseline", default=None,
                        help="Snapshot label to compare against, e.g. pre-v3-engine.")
    parser.add_argument("--show-errors", type=int, default=10,
                        help="How many FP/FN rows to print (default 10).")
    args = parser.parse_args()

    conn = db.get_connection()
    try:
        rows = fetch(conn, args.category)
        if not rows:
            print("No rows matched.")
            return 1

        truth = fetch_crosswalk(conn, args.category)
        scope = args.category or "all categories"
        labelled = [r for r in rows if (r["review_status"] or "").strip()]

        print()
        print("=" * 68)
        print(f"MATCH METRICS -- {scope}")
        print("=" * 68)
        print(f"  rows                    : {len(rows)}")
        print(f"  with a steward verdict  : {len(labelled)}")
        print(f"  with a CORRECT code     : {len(truth)}   (from crosswalk)")
        print(f"  unlabelled              : {len(rows) - len(labelled)}")

        print()
        print("  current tier distribution:")
        for tier, n in Counter(r["mapping_status"] for r in rows).most_common():
            print(f"     {str(tier):<24}{n:>5}  ({100 * n / len(rows):.0f}%)")

        counts = confusion(rows, truth)
        total = sum(counts.values())
        if not total:
            print()
            print("  No steward verdicts in scope -- nothing to measure against.")
            print("  TP/FP/TN/FN need review_status of Approved or Rejected.")
            return 0

        metrics = rates(counts)
        print()
        print("  confusion matrix:")
        print(f"     TP  engine picked the steward's code   {counts['TP']:>5}")
        print(f"     FP  engine confident but WRONG code    {counts['FP']:>5}   <- ships a bad mapping")
        print(f"     TN  rejected + engine withheld         {counts['TN']:>5}")
        print(f"     FN  right answer exists, engine unsure {counts['FN']:>5}   <- match lost")
        print(f"     {'total':<34}{total:>5}")
        print()
        print(f"     precision  {_pct(metrics['precision'])}"
              "   of the matches asserted, how many were right")
        print(f"     recall     {_pct(metrics['recall'])}"
              "   of the approved matches, how many we still assert")
        print(f"     F1         {_pct(metrics['f1'])}")
        print()
        print("     NOTE: measured only where a steward recorded the correct code.")
        print("     Rejections contribute FP/TN but not precision -- a rejection says")
        print("     the candidate was wrong, not which product was right.")

        print()
        print("  what decided each row:")
        for method, n in Counter(str(r["match_method"]) for r in rows).most_common():
            print(f"     {method:<24}{n:>5}")

        if args.show_errors:
            fps = [r for r in rows
                   if (t := truth.get(f"{r['channel']}|{r['sku']}")) is not None
                   and r["mapping_status"] in CONFIDENT
                   and (r["product_code"] or "").strip() != t]
            fns = [r for r in rows
                   if truth.get(f"{r['channel']}|{r['sku']}") is not None
                   and r["mapping_status"] not in CONFIDENT]
            for kind, bad in (("FALSE POSITIVES (wrong code)", fps),
                              ("LOST MATCHES (FN)", fns)):
                if not bad:
                    continue
                print()
                print(f"  {kind} ({len(bad)}):")
                for row in bad[: args.show_errors]:
                    key = f"{row['channel']}|{row['sku']}"
                    print(f"     [{row['channel']}] {str(row['sku'])[:18]:<20}{row['mapping_status']}")
                    print(f"        listing : {str(row['title'])[:62]}")
                    print(f"        engine  : {row['product_code']} {str(row['product_name'])[:44]}")
                    print(f"        steward : {truth.get(key)}")
                    print(f"        {row['identity_evidence'] or '(no identity evidence)'}")

        if args.baseline:
            print()
            print("=" * 68)
            print(f"OLD vs NEW  (baseline '{args.baseline}')")
            print("=" * 68)
            where_cat = "and b.category = :cat" if args.category else ""
            params = {"label": args.baseline}
            if args.category:
                params["cat"] = args.category
            old = {
                f"{r['channel']}|{r['sku']}": r
                for r in conn.execute(sa.text(f"""
                    select channel, sku, old_mapping_status, old_product_code,
                           old_review_status
                    from   audit.mapping_baseline b
                    where  b.snapshot_label = :label and b.match_rank = 1 {where_cat}
                """), params).mappings()
            }
            if not old:
                print(f"  No baseline rows for '{args.baseline}'.")
                return 0

            # The OLD engine had no identity layer, so its confident tiers are
            # the same two -- the comparison is like for like.
            old_counts = {"TP": 0, "FP": 0, "TN": 0, "FN": 0}
            for row in rows:
                key = f"{row['channel']}|{row['sku']}"
                prior = old.get(key)
                if prior is None:
                    continue
                correct = truth.get(key)
                asserted = prior["old_mapping_status"] in CONFIDENT
                picked = (prior["old_product_code"] or "").strip()
                if correct is not None:
                    if not asserted:
                        old_counts["FN"] += 1
                    elif picked == correct:
                        old_counts["TP"] += 1
                    else:
                        old_counts["FP"] += 1
                elif (prior["old_review_status"] or "").strip().lower() == "rejected":
                    old_counts["FP" if asserted else "TN"] += 1

            old_rates = rates(old_counts)
            print(f"  {'':<12}{'OLD':>8}{'NEW':>8}{'CHANGE':>10}")
            for key in ("TP", "FP", "TN", "FN"):
                delta = counts[key] - old_counts[key]
                print(f"  {key:<12}{old_counts[key]:>8}{counts[key]:>8}"
                      f"{('+' if delta > 0 else '') + str(delta):>10}")
            print()
            for key in ("precision", "recall", "f1"):
                print(f"  {key:<12}{_pct(old_rates[key]):>8}{_pct(metrics[key]):>8}")

            moved = sum(
                1 for row in rows
                if (p := old.get(f"{row['channel']}|{row['sku']}"))
                and p["old_product_code"] != row["product_code"]
            )
            print()
            print(f"  rows now matching a DIFFERENT product: {moved}")

        print()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
