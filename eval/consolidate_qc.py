"""Merge the QC workbooks into ONE ground-truth file.

    python -m eval.consolidate_qc --qc-dir "D:/Himalaya/QC_data"

Reads every *.xlsx in the QC directory and writes a single normalised table to
eval/qc_ground_truth.csv. Nothing touches the database -- most of these SKUs
have not been run yet, so joining against live mapping rows would silently drop
them. This step is purely "what did the reviewer say is correct".

The workbooks carry two shapes, from two rounds of review:

  *_QC_Result.xlsx   the superset -- has 'Correct at rank'
  *_QCed.xlsx        the same rows, fewer columns, no rank

and Set 1 (Vitamins) predates the R1/R2/R3 layout entirely: it shows a single
engine pick under 'Mapped code'. All three are normalised to the same output.

GROUND TRUTH is derived from the reviewer's two columns, never guessed:

    QC: Correct? = YES/CORRECT, Correct at rank = 1   -> R1 code
    QC: Correct? = YES,         Correct at rank = 2/3 -> R2/R3 code
    QC: Correct? = NO/INCORRECT, More accurate code   -> that code
    QC: Correct? = NO,           no code given        -> no valid mapping

That last case is a real answer, not a gap: the reviewer looked and found
nothing in the master worth mapping to. It is kept with gt_source='none' so a
later scoring pass can tell "we have no truth for this row" apart from "the
truth is that nothing matches" -- the engine should be scored for correctly
returning NoHimalayaEquivalent there, not penalised for it.

Rows are de-duplicated on (channel, sku), preferring the _QC_Result variant
because it carries the rank column. Where the same SKU is QC'd in two
different category files, both are kept in the report and flagged.
"""
from __future__ import annotations

import argparse
import glob
import os
import re

import pandas as pd

# Column aliases. The workbooks were authored by hand over several rounds, so
# the same field has different names per set -- resolved here rather than with
# a per-file lookup, which would need editing every time a new set arrives.
TITLE_COLS = ("1DS title", "title", "Title")
R1_COLS = ("R1 code", "Mapped code")
R1_NAME_COLS = ("R1 product_name", "Mapped product_name")

# 'CORRECT'/'INCORRECT' appear in the earlier sets, 'YES'/'NO' in the later
# ones. Same meaning; normalised so downstream code tests one vocabulary.
YES = {"YES", "Y", "CORRECT", "TRUE"}
NO = {"NO", "N", "INCORRECT", "FALSE"}


def _first(frame: pd.DataFrame, names) -> str | None:
    for name in names:
        if name in frame.columns:
            return name
    return None


def _clean_code(value) -> str | None:
    """Product codes to a canonical string.

    Excel reads a numeric-looking code as a float, so '7000679' arrives as
    7000679.0 and every string comparison against the master fails. Codes are
    also written with stray notes ("7000679 (check)"), so the digits are
    extracted rather than trusted whole.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "-", "na", "n/a"):
        return None
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    match = re.search(r"\d{6,8}", text)
    return match.group(0) if match else None


def _text(value) -> str:
    """A cell to a trimmed string, with pandas' NaN rendered as empty."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none") else text


def _verdict(value) -> str:
    text = str(value).strip().upper()
    if text in YES:
        return "correct"
    if text in NO:
        return "incorrect"
    return "unknown"


def _rank(value) -> int | None:
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return number if number in (1, 2, 3) else None


def _category_from_filename(path: str) -> str:
    """'Set 12 Face Care_QC_Result.xlsx' -> 'Face Care'."""
    stem = os.path.splitext(os.path.basename(path))[0]
    stem = re.sub(r"_?QC[_ ]?Result", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"_?QCed", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"^Set\s*\d+\s*[_-]?\s*", "", stem, flags=re.IGNORECASE)
    return stem.strip(" _-") or stem


def extract_sheet(frame: pd.DataFrame, path: str, sheet: str) -> pd.DataFrame | None:
    """One QC sheet to the common shape, or None if it is not a QC sheet.

    Two workbooks (Lip Care, Herbal Supplements) carry an extra 'Sheet1' tab
    holding a master extract rather than QC output. Detected by the absence of
    sku/verdict rather than by name, so any future stray tab is skipped too.
    """
    if "sku" not in frame.columns or "QC: Correct?" not in frame.columns:
        return None

    title_col = _first(frame, TITLE_COLS)
    r1_col = _first(frame, R1_COLS)
    r1_name_col = _first(frame, R1_NAME_COLS)
    has_rank = "Correct at rank" in frame.columns

    rows = []
    for _, row in frame.iterrows():
        sku = str(row["sku"]).strip() if pd.notna(row.get("sku")) else ""
        if not sku or sku.lower() == "nan":
            continue

        verdict = _verdict(row.get("QC: Correct?"))
        ranked = {
            1: _clean_code(row.get(r1_col)) if r1_col else None,
            2: _clean_code(row.get("R2 code")),
            3: _clean_code(row.get("R3 code")),
        }
        # Names are carried per rank so the reported product_name always
        # belongs to the code that was judged correct. Taking R1's name for a
        # row whose truth is R2 would pair the right code with the wrong
        # description -- the kind of error a reader trusts and cannot see.
        ranked_names = {
            1: _text(row.get(r1_name_col)) if r1_name_col else "",
            2: _text(row.get("R2 product_name")),
            3: _text(row.get("R3 product_name")),
        }
        better = _clean_code(row.get("More accurate code"))
        better_name = _text(row.get("More accurate product_name"))

        # Resolve the reviewer's answer to one product_code, and carry the
        # matching name with it.
        truth, truth_name, source = None, "", "none"
        if verdict == "correct":
            at = _rank(row.get("Correct at rank")) if has_rank else 1
            # A _QCed file has no rank column: "correct" there can only mean
            # the pick it was shown, which is rank 1.
            at = at or 1
            truth = ranked.get(at)
            truth_name = ranked_names.get(at, "")
            source = f"rank{at}" if truth else "none"
            if truth is None and better:
                # Marked correct but the rank cell points at an empty code --
                # fall back to the reviewer's own suggestion rather than
                # dropping a row they did answer.
                truth, truth_name, source = better, better_name, "more_accurate"
        elif verdict == "incorrect" and better:
            truth, truth_name, source = better, better_name, "more_accurate"
        # verdict == incorrect with no suggestion stays (None, "none"):
        # the reviewer looked and found nothing worth mapping to.

        rows.append(
            {
                "channel": str(row.get("channel", "")).strip().lower(),
                "sku": sku,
                "category": _category_from_filename(path),
                "title": str(row.get(title_col, "")).strip() if title_col else "",
                "qc_verdict": verdict,
                "gt_product_code": truth,
                "gt_product_name": truth_name,
                "gt_source": source,
                "engine_r1_code": ranked[1],
                "engine_r1_name": (
                    str(row.get(r1_name_col, "")).strip() if r1_name_col else ""
                ),
                "engine_r2_code": ranked[2],
                "engine_r3_code": ranked[3],
                "qc_reason": str(row.get("Reason", "")).strip(),
                "source_file": os.path.basename(path),
                "source_sheet": sheet,
                "has_rank_column": has_rank,
            }
        )

    return pd.DataFrame(rows) if rows else None


def _attach_source_categories(frame: pd.DataFrame, skip_db: bool) -> pd.DataFrame:
    """Add the 1DS category / sub-category for each SKU from staging.

    READ ONLY -- one SELECT per channel, no writes, no LLM. The QC workbooks
    do not carry a sub-category at all and only some carry a category, so
    staging is the only complete source. A SKU that staging does not know
    keeps an empty pair rather than being dropped: this lookup enriches the
    ground truth, it must never filter it.

    A SKU can appear on several staging rows (re-ingests over time). They agree
    on category, so the first is taken.
    """
    frame = frame.copy()
    frame["oneds_category"] = ""
    frame["oneds_subcategory"] = ""
    if skip_db:
        return frame

    try:
        from common import db, db_models
        import sqlalchemy as sa
    except ImportError:
        print("  (staging lookup skipped: database modules unavailable)")
        return frame

    try:
        conn = db.get_connection()
    except Exception as exc:                                  # noqa: BLE001
        print(f"  (staging lookup skipped: {type(exc).__name__}: {exc})")
        return frame

    try:
        for channel in frame["channel"].dropna().unique():
            skus = frame.loc[frame["channel"] == channel, "sku"].unique().tolist()
            if not skus:
                continue
            try:
                table = db_models.resolve_channel_tables(conn.engine, channel).products
            except Exception:                                 # noqa: BLE001
                continue                    # a channel with no staging table
            lookup: dict[str, tuple[str, str]] = {}
            # SQL Server caps bound parameters at 2100, so the IN list is
            # chunked well below that.
            for start in range(0, len(skus), 500):
                rows = conn.execute(
                    sa.select(table.c.sku, table.c.category, table.c.subcategory)
                    .where(table.c.sku.in_(skus[start:start + 500]))
                ).all()
                for sku, category, subcategory in rows:
                    lookup.setdefault(
                        str(sku), (str(category or ""), str(subcategory or ""))
                    )
            mask = frame["channel"] == channel
            frame.loc[mask, "oneds_category"] = (
                frame.loc[mask, "sku"].map(lambda s: lookup.get(s, ("", ""))[0])
            )
            frame.loc[mask, "oneds_subcategory"] = (
                frame.loc[mask, "sku"].map(lambda s: lookup.get(s, ("", ""))[1])
            )
    finally:
        conn.close()

    return frame


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--qc-dir", default=r"D:/Himalaya/QC_data")
    parser.add_argument("--out", default="eval/qc_ground_truth.csv")
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="skip the staging lookup for 1DS category/sub-category",
    )
    args = parser.parse_args()

    paths = sorted(glob.glob(os.path.join(args.qc_dir, "*.xlsx")))
    if not paths:
        raise SystemExit(f"no .xlsx found in {args.qc_dir}")

    frames, skipped = [], []
    for path in paths:
        book = pd.ExcelFile(path)
        for sheet in book.sheet_names:
            frame = pd.read_excel(path, sheet_name=sheet)
            extracted = extract_sheet(frame, path, sheet)
            if extracted is None:
                skipped.append(f"{os.path.basename(path)} :: {sheet}")
                continue
            frames.append(extracted)

    merged = pd.concat(frames, ignore_index=True)

    # Prefer the _QC_Result variant of a duplicated row: same SKU, but it
    # carries the rank column, so its ground truth can distinguish "rank 2 was
    # right" from a bare "correct".
    merged["_priority"] = merged["has_rank_column"].map({True: 0, False: 1})
    merged = merged.sort_values(["channel", "sku", "_priority"])

    before = len(merged)
    deduped = merged.drop_duplicates(subset=["channel", "sku"], keep="first")
    deduped = deduped.drop(columns=["_priority"])

    # Two rows can disagree for two very different reasons, and only one is a
    # problem:
    #
    #   same set  -- the _QCed and _QC_Result copies of one review. _QCed has
    #                no rank column, so a row the reviewer marked correct AT
    #                RANK 2 reads there as plain "correct" and resolves to
    #                rank 1. Not a disagreement between reviewers, just a
    #                lossier file; the de-dupe above already keeps the
    #                _QC_Result answer. Measured: all 36 are this.
    #
    #   across sets -- the same SKU reviewed under two categories with two
    #                different answers. That IS a real conflict and needs a
    #                human, so it is reported separately and loudly.
    disagreeing = (
        merged.groupby(["channel", "sku"])["gt_product_code"]
        .nunique(dropna=True)
        .loc[lambda s: s > 1]
    )

    def _set_number(filename: str) -> str:
        match = re.match(r"Set\s*(\d+)", filename)
        return match.group(1) if match else filename

    same_set, cross_set = 0, []
    for channel, sku in disagreeing.index:
        rows = merged[(merged["channel"] == channel) & (merged["sku"] == sku)]
        if rows["source_file"].map(_set_number).nunique() == 1:
            same_set += 1
        else:
            cross_set.append((channel, sku))

    # 1DS category/subcategory come from the staging tables, not the QC
    # workbooks: only some sets carry a '1DS category' column and none carries
    # a sub-category, while staging holds both for every SKU. This is a
    # read-only lookup -- the QC rows themselves are never filtered by it, so a
    # SKU missing from staging keeps its filename-derived category and an empty
    # sub-category rather than being dropped.
    deduped = _attach_source_categories(deduped, args.no_db)

    # Written only AFTER the categories are attached: an earlier version wrote
    # here first, so the detailed CSV shipped with both columns empty while the
    # xlsx had them, and every per-category report came out blank.
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    deduped.to_csv(args.out, index=False, encoding="utf-8-sig")

    # The reader's sheet: the answer only, no audit trail. Rows where the
    # reviewer found NO valid mapping are excluded -- a blank product_code in a
    # file called "correct mapping" reads as missing data when it is really a
    # decision. They stay in the detailed CSV with gt_source='none'.
    simple = (
        deduped.loc[deduped["gt_product_code"].notna(),
                    ["sku", "title", "channel", "oneds_category",
                     "oneds_subcategory", "gt_product_code",
                     "gt_product_name", "category"]]
        .rename(columns={"gt_product_code": "correct_product_code",
                         "gt_product_name": "correct_product_name",
                         "category": "qc_set"})
        .sort_values(["oneds_category", "oneds_subcategory", "channel", "sku"])
    )
    simple_path = os.path.splitext(args.out)[0] + "_simple.xlsx"
    try:
        simple.to_excel(simple_path, index=False, sheet_name="Correct Mappings")
    except (ImportError, ValueError):
        # openpyxl missing -- a CSV carries the same four columns.
        simple_path = os.path.splitext(args.out)[0] + "_simple.csv"
        simple.to_csv(simple_path, index=False, encoding="utf-8-sig")

    usable = deduped["gt_product_code"].notna().sum()
    print(f"workbooks read        : {len(paths)}")
    print(f"QC sheets extracted   : {len(frames)}")
    print(f"rows before de-dupe   : {before}")
    print(f"rows written          : {len(deduped)}")
    print(f"  with a ground truth : {usable}")
    print(f"  'no valid mapping'  : {(deduped['gt_source'] == 'none').sum()}")
    print(f"resolved (QCed vs QC_Result, kept the ranked answer): {same_set}")
    print(f"UNRESOLVED conflicts across different sets          : {len(cross_set)}")
    print(f"\nby gt_source:")
    for name, count in deduped["gt_source"].value_counts().items():
        print(f"   {name:<16}{count}")
    print(f"\nby channel:")
    for name, count in deduped["channel"].value_counts().items():
        print(f"   {name or '(blank)':<16}{count}")
    if skipped:
        print(f"\nnon-QC sheets skipped ({len(skipped)}):")
        for name in skipped:
            print(f"   {name}")
    if cross_set:
        print(f"\nNEEDS A HUMAN -- same SKU, different answer in different sets:")
        for channel, sku in cross_set[:20]:
            rows = merged[(merged["channel"] == channel) & (merged["sku"] == sku)]
            print(f"   {channel} {sku}")
            for _, row in rows.iterrows():
                print(
                    f"      {row['source_file'][:46]:<48}"
                    f"gt={row['gt_product_code']} ({row['gt_source']})"
                )
    print(f"\nwritten: {args.out}")


if __name__ == "__main__":
    main()
