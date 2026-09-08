"""Score every engine version against the data engineer's validated sheet.

The validation workbook's 'Consolidated' sheet puts four engine runs side by
side (V1 Previous, V1 Current, V2, V3 Engine), keyed by SKU, with a
'Comment: Correct mapping' column naming which engines got that row right.
This script derives the ground-truth product_code from that comment, pulls
the CURRENT engine's rank-1 pick straight out of the database, and reports
top-1 accuracy for all of them on the same rows.

    python -m eval.score_against_validated --workbook <path.xlsx>

Everything is done in one pass, in memory. An earlier version of this
round-tripped intermediate frames through CSV and silently turned
product_code into a float ('7000679' -> '7000679.0'), which made every
string comparison false and produced a table of zeroes. Codes are normalised
through one helper here, once.
"""
from __future__ import annotations

import argparse
import os

import pandas as pd

from common import db

CHANNELS = ["amazon", "blinkit", "swiggy", "zepto"]

# Column suffixes pandas assigns to the four repeated engine blocks.
ENGINES = [
    ("V1 Previous", ""),
    ("V1 Current", ".1"),
    ("V2", ".2"),
    ("V3 (sheet)", ".3"),
]


def norm_code(series: pd.Series) -> pd.Series:
    """Product codes to a canonical string.

    They arrive as a mix of int, float and str across sheets ('7000679',
    7000679, 7000679.0). Comparing those directly is how a scoring script
    quietly reports nonsense, so everything goes through here.
    """
    numeric = pd.to_numeric(series, errors="coerce")
    out = numeric.astype("Int64").astype(str)
    return out.replace("<NA>", "").str.strip()


def load_current_run(category: str) -> pd.DataFrame:
    """This engine's rank-1 pick per SKU, for one 1DS category."""
    conn = db.get_connection()
    try:
        frames = []
        for channel in CHANNELS:
            id_col = f"{channel}_product_id"
            frames.append(pd.read_sql(
                f"""
                SELECT p.sku, m.product_code, m.product_name,
                       m.ensemble_score, m.final_score, p.mapping_status
                FROM staging.{channel}_products p
                JOIN staging.{channel}_product_mapping m
                  ON m.{id_col} = p.id
                WHERE LOWER(p.brand) LIKE '%himalaya%'
                  AND LOWER(p.category) = ?
                  AND m.match_rank = 1
                """,
                conn.engine, params=(category,),
            ))
        return pd.concat(frames, ignore_index=True)
    finally:
        conn.close()


def derive_truth(row: pd.Series) -> str:
    """Ground-truth code from the reviewer's comment.

    The comment names which engines were right ("V1 Current & V2 & V3
    Engine"), so the correct code is whichever named engine's pick is. They
    agree by construction when several are named -- take the first present.
    Blank and "Wrong" mean no engine was right, so the row cannot score
    anyone and is excluded.
    """
    label = row["_label"]
    if label in ("", "wrong", "nan"):
        return ""

    if label.startswith("all"):
        return row["_code.3"] or row["_code.2"]

    for key, suffix in (("v2", ".2"), ("v1", ".1"), ("v3", ".3")):
        if key in label:
            code = row[f"_code{suffix}"]
            if code:
                return code
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", required=True)
    parser.add_argument("--sheet", default="Consolidated")
    parser.add_argument("--category", default="lip makeup")
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
        current[["_sku", "_code_now", "mapping_status"]], on="_sku", how="left"
    )
    merged["_code_now"] = merged["_code_now"].fillna("")

    scored = merged[merged["_truth"] != ""].copy()

    print(f"\nCategory      : {args.category}")
    print(f"Rows in sheet : {len(merged)}")
    print(f"Scoreable     : {len(scored)}  "
          f"(excludes blank / 'Wrong' -- no engine was right on those)")
    print(f"Matched to DB : {(merged['_code_now'] != '').sum()}\n")

    columns = [(name, f"_code{suffix}") for name, suffix in ENGINES]
    columns.append(("V3 (CURRENT RUN)", "_code_now"))

    print(f"{'Engine':<22}{'correct':>9}{'accuracy':>11}")
    print("-" * 43)
    correct: dict[str, pd.Series] = {}
    for name, column in columns:
        hit = scored[column].astype(str) == scored["_truth"].astype(str)
        correct[name] = hit
        print(f"{name:<22}{hit.sum():>9}{100.0 * hit.mean():>10.1f}%")

    best_prior = correct["V2"]
    now = correct["V3 (CURRENT RUN)"]
    print(f"\nCurrent run vs V2:")
    print(f"  V2 right, current wrong : {(best_prior & ~now).sum()}")
    print(f"  current right, V2 wrong : {(now & ~best_prior).sum()}")

    regressions = scored[best_prior & ~now]
    if not regressions.empty:
        print("\nRows V2 gets right and the current run does not:\n")
        for _, row in regressions.iterrows():
            print(f"  {str(row['title.3'])[:64]}")
            print(f"     truth={row['_truth']}  current={row['_code_now']}")

    out = os.path.join(os.path.dirname(__file__), "snapshots",
                       f"scored_{args.category.replace(' ', '_')}.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    scored.to_csv(out, index=False)
    print(f"\nDetail written to {out}\n")


if __name__ == "__main__":
    main()
