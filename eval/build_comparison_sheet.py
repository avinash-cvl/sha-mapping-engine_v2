"""Build a reviewable comparison workbook: every engine side by side.

Adds a 'V3 Current' block (this engine, read live from the database) next to
the four runs already in the validated workbook, scores each against the
ground truth derived from the reviewer's comment, and writes a workbook a
human can sort and filter.

    python -m eval.build_comparison_sheet \
        --workbook <validated.xlsx> --out <comparison.xlsx>

Sheets produced:
  Summary      -- accuracy per engine, and the fixed/broken counts
  Comparison   -- one row per SKU, every engine's pick, who was right
  Changed      -- only rows where V3 Current differs from the V3 baseline
  Still Wrong  -- rows V3 Current gets wrong, for the next iteration
  Truth Check  -- whether each ground-truth code exists in the master
"""
from __future__ import annotations

import argparse
import os

import pandas as pd

from common import db
from eval.score_against_validated import (
    CHANNELS,
    ENGINES,
    derive_truth,
    load_current_run,
    norm_code,
)


def master_codes() -> set[str]:
    """Every product_code in the master, to check truth values are real."""
    conn = db.get_connection()
    try:
        rows = conn.exec_driver_sql(
            "SELECT product_code FROM staging.himalaya_products "
            "WHERE product_code IS NOT NULL"
        ).fetchall()
        return {str(r[0]).strip() for r in rows}
    finally:
        conn.close()


def master_names() -> dict[str, str]:
    conn = db.get_connection()
    try:
        rows = conn.exec_driver_sql(
            "SELECT product_code, normalized_title FROM staging.himalaya_products "
            "WHERE product_code IS NOT NULL"
        ).fetchall()
        return {str(r[0]).strip(): str(r[1]) for r in rows}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", required=True)
    parser.add_argument("--sheet", default="Consolidated")
    parser.add_argument("--category", default="lip makeup")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    sheet = pd.read_excel(args.workbook, sheet_name=args.sheet, header=1)
    sheet = sheet[sheet["match_rank.3"] == 1].copy()

    sheet["_label"] = (
        sheet["Comment: Correct mapping"].fillna("").astype(str).str.strip().str.lower()
    )
    for _, suffix in ENGINES:
        sheet[f"_code{suffix}"] = norm_code(sheet[f"product_code{suffix}"])
    sheet["_truth"] = sheet.apply(derive_truth, axis=1)
    sheet["_sku"] = sheet["sku.3"].astype(str).str.strip()

    current = load_current_run(args.category)
    current["_sku"] = current["sku"].astype(str).str.strip()
    current["_code_now"] = norm_code(current["product_code"])
    current = current.drop_duplicates("_sku")

    merged = sheet.merge(
        current[["_sku", "_code_now", "product_name", "ensemble_score",
                 "final_score", "mapping_status"]],
        on="_sku", how="left",
    )
    merged["_code_now"] = merged["_code_now"].fillna("")

    names = master_names()
    codes = master_codes()

    columns = [(name, f"_code{suffix}") for name, suffix in ENGINES]
    columns.append(("V3 Current", "_code_now"))

    out = pd.DataFrame({
        "channel": merged["channel.3"],
        "sku": merged["_sku"],
        "title": merged["title.3"],
        "reviewer_comment": merged["Comment: Correct mapping"],
        "truth_code": merged["_truth"],
        "truth_name": merged["_truth"].map(lambda c: names.get(str(c), "")),
    })

    for name, column in columns:
        out[f"{name} pick"] = merged[column]
        out[f"{name} name"] = merged[column].map(lambda c: names.get(str(c), ""))
        out[f"{name} OK"] = [
            "" if not t else ("YES" if str(p) == str(t) else "no")
            for p, t in zip(merged[column], merged["_truth"])
        ]

    out["V3 Current status"] = merged["mapping_status"]
    out["V3 Current ensemble"] = merged["ensemble_score"]
    out["V3 Current final"] = merged["final_score"]

    scoreable = out[out["truth_code"] != ""].copy()

    rows = []
    for name, _ in columns:
        hits = (scoreable[f"{name} OK"] == "YES").sum()
        rows.append({
            "Engine": name,
            "Correct": hits,
            "Scored rows": len(scoreable),
            "Accuracy %": round(100.0 * hits / len(scoreable), 1),
        })
    summary = pd.DataFrame(rows)

    base_ok = scoreable["V3 (sheet) OK"] == "YES"
    now_ok = scoreable["V3 Current OK"] == "YES"
    summary_notes = pd.DataFrame([
        {"Metric": "V3 baseline correct", "Value": int(base_ok.sum())},
        {"Metric": "V3 Current correct", "Value": int(now_ok.sum())},
        {"Metric": "Net change", "Value": int(now_ok.sum() - base_ok.sum())},
        {"Metric": "Fixed (baseline wrong -> current right)",
         "Value": int((now_ok & ~base_ok).sum())},
        {"Metric": "Broken (baseline right -> current wrong)",
         "Value": int((base_ok & ~now_ok).sum())},
        {"Metric": "Both wrong", "Value": int((~base_ok & ~now_ok).sum())},
        {"Metric": "Rows excluded (blank / 'Wrong' comment)",
         "Value": int(len(out) - len(scoreable))},
    ])

    changed = out[out["V3 (sheet) pick"].astype(str) != out["V3 Current pick"].astype(str)]
    still_wrong = scoreable[scoreable["V3 Current OK"] == "no"]

    truth_check = pd.DataFrame({
        "sku": scoreable["sku"],
        "title": scoreable["title"],
        "truth_code": scoreable["truth_code"],
        "exists_in_master": [
            "YES" if str(c) in codes else "NO -- NOT A REAL PRODUCT CODE"
            for c in scoreable["truth_code"]
        ],
        "reviewer_comment": scoreable["reviewer_comment"],
    })

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with pd.ExcelWriter(args.out, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False, startrow=0)
        summary_notes.to_excel(writer, sheet_name="Summary", index=False,
                               startrow=len(summary) + 3)
        out.to_excel(writer, sheet_name="Comparison", index=False)
        changed.to_excel(writer, sheet_name="Changed", index=False)
        still_wrong.to_excel(writer, sheet_name="Still Wrong", index=False)
        truth_check.to_excel(writer, sheet_name="Truth Check", index=False)

    print(summary.to_string(index=False))
    print()
    print(summary_notes.to_string(index=False))
    print()
    bad = (truth_check["exists_in_master"] != "YES").sum()
    print(f"Truth codes not present in the master: {bad}")
    print(f"\nWritten to {args.out}")


if __name__ == "__main__":
    main()
