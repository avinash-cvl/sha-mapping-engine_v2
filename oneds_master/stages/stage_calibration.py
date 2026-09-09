"""TP/FP calibration -- turns two disagreeing scores into one measured probability.

Every scored row carries two independent opinions: the deterministic
`ensemble_score` from stage_scoring.py, and the MCDA judge's `llm_confidence`.
Neither is reliable alone, and both failure directions are measured on live
data:

    B0CD466FHY  "tan removal orange peel off mask, 8gm, Pack of 12"
                ensemble 0.23, LLM 0.99 -> the LLM was RIGHT (exact product,
                size and count; the ensemble ranked a turmeric face pack above
                the correct row)

    B000N33STO  "Skin Wellness Tablets - 60 Count (Neem)"
                ensemble 0.71, LLM 0.94 -> the LLM was WRONG (chose the
                500-count master over the correct 60-count)

determine_mapping_status() currently arbitrates with max(ensemble, llm) plus a
hand-set 0.60 floor. No constant can be right for both rows above, and that one
was never validated against outcomes.

This module replaces the constant with evidence. Rows are bucketed by
(ensemble band, LLM band); each bucket's calibrated score is the share of
human-reviewed rows in it that a steward APPROVED. That share is a genuine
probability -- 0.84 means "rows that looked like this were correct 84% of the
time" -- which is exactly what the weighted average it replaces is not.

Nothing here changes behaviour until a bucket has real labels behind it:
lookup() returns None for an inactive or unknown bucket, and the caller keeps
its existing logic. See sql/012_calibration_buckets.sql.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import sqlalchemy as sa

import common.config as C
from common import db

logger = logging.getLogger(__name__)

# Band boundaries. Deliberately the SAME cutoffs determine_mapping_status()
# uses for its tiers, so a bucket lines up with the decision it informs -- a
# separate set would make "the mid/high bucket" mean one thing here and another
# downstream. Stored on each bucket row too, so a later retune is detectable
# rather than silently invalidating every stored rate.
BAND_HIGH = 0.86
BAND_MID = 0.61


def band(score: float | None) -> str:
    """Score -> 'low' | 'mid' | 'high' | 'none'.

    NaN and None both become 'none', which is a real bucket rather than a
    missing one: a row the judge never ran on (needs_judge() was false) is
    ensemble-only evidence, and lumping it in with a judged bucket would
    inflate that bucket's apparent reliability with rows the judge never saw.
    """
    if score is None:
        return "none"
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "none"
    if math.isnan(value):
        return "none"
    if value >= BAND_HIGH:
        return "high"
    if value >= BAND_MID:
        return "mid"
    return "low"


def bucket_key(ensemble_score: float | None, llm_confidence: float | None) -> str:
    """The stored 'ens/llm' key, e.g. 'low/high'."""
    return f"{band(ensemble_score)}/{band(llm_confidence)}"


def wilson_lower_bound(successes: int, total: int, z: float = 1.96) -> float:
    """Lower bound of the Wilson 95% interval for a proportion.

    Used INSTEAD of the raw rate when deciding whether a bucket may auto-match.
    The raw rate cannot distinguish 3/3 from 300/300 -- both are 1.0 -- and
    acting on the first would be acting on nothing. Wilson pulls small samples
    toward 0.5 in proportion to their thinness, so a bucket has to earn its
    confidence with volume as well as accuracy.
    """
    if total <= 0:
        return 0.0
    phat = successes / total
    denom = 1.0 + (z * z) / total
    centre = phat + (z * z) / (2 * total)
    margin = z * math.sqrt((phat * (1.0 - phat) + (z * z) / (4 * total)) / total)
    return max(0.0, (centre - margin) / denom)


@dataclass(frozen=True)
class Calibration:
    """One bucket's measured reliability."""

    bucket: str
    precision: float
    precision_lower_95: float
    n_labelled: int
    n_approved: int


def load_table(conn: db.Connection) -> dict[str, Calibration]:
    """Reads the ACTIVE buckets into a dict keyed 'ens/llm'.

    Returns {} -- meaning "calibration is off, use the existing logic" -- when
    the table does not exist yet. That is the state before migration 012 is
    applied, and it must not be an error: this module is additive and the
    pipeline has to run identically without it.
    """
    try:
        rows = conn.execute(
            sa.text(
                """
                select ensemble_band, llm_band, precision_rate,
                       precision_lower_95, n_labelled, n_approved
                from   config.calibration_buckets
                where  is_active = 1
                and    precision_rate is not null
                """
            )
        ).fetchall()
    except Exception as exc:  # table absent, or no permission
        logger.info("Calibration table unavailable (%s) -- using existing logic", exc)
        return {}

    table = {
        f"{row[0]}/{row[1]}": Calibration(
            bucket=f"{row[0]}/{row[1]}",
            precision=float(row[2]),
            precision_lower_95=float(row[3]) if row[3] is not None else 0.0,
            n_labelled=int(row[4]),
            n_approved=int(row[5]),
        )
        for row in rows
    }
    if table:
        logger.info(
            "Calibration active for %d bucket(s): %s",
            len(table),
            ", ".join(sorted(table)),
        )
    else:
        logger.info("No calibration bucket is active yet -- using existing logic")
    return table


def lookup(
    table: dict[str, Calibration],
    ensemble_score: float | None,
    llm_confidence: float | None,
) -> Calibration | None:
    """The calibration for this pair of scores, or None to fall back.

    None is returned for an unknown or inactive bucket, which is the common
    case early on -- most buckets will not have 30 labels for some time. The
    caller must treat None as "decide the way you did before".
    """
    if not table:
        return None
    return table.get(bucket_key(ensemble_score, llm_confidence))


def rebuild(
    conn: db.Connection,
    min_labels: int = 30,
    note: str | None = None,
    dry_run: bool = True,
) -> list[dict[str, object]]:
    """Recomputes every bucket from the human verdicts currently in staging.

    A row counts only when BOTH halves are present and comparable: a
    review_status of Approved/Rejected, and a rank-1 mapping row carrying the
    scores that verdict was formed against.

    The comparability caveat is the one that matters. A steward's verdict is
    about the candidate they were shown, so it is evidence only about the
    scores from THAT run. If the engine is re-run and a row's rank-1 changes,
    the old verdict no longer describes the new pairing -- it has to be
    re-reviewed or dropped. `note` exists to record which run the counts came
    from; mixing runs is the main way a calibration table goes quietly wrong.

    dry_run=True by default: returns what it would write and touches nothing.
    """
    channels = ("amazon", "blinkit", "swiggy", "zepto")

    union = " union all ".join(
        f"""
        select p.review_status, m.ensemble_score, m.final_score
        from   staging.{ch}_products p
        join   (select source_sku, ensemble_score, final_score,
                       row_number() over (partition by source_sku
                                          order by created_at desc) rn
                from   staging.{ch}_product_mapping
                where  match_rank = 1) m
          on   m.source_sku = p.sku and m.rn = 1
        where  p.review_status is not null
        """
        for ch in channels
    )

    rows = conn.execute(sa.text(union)).fetchall()

    tally: dict[str, dict[str, int]] = {}
    for review_status, ensemble_score, llm_confidence in rows:
        verdict = (review_status or "").strip().upper()
        if verdict not in ("APPROVED", "REJECTED"):
            continue
        key = bucket_key(
            float(ensemble_score) if ensemble_score is not None else None,
            float(llm_confidence) if llm_confidence is not None else None,
        )
        counts = tally.setdefault(key, {"n": 0, "approved": 0, "rejected": 0})
        counts["n"] += 1
        counts["approved" if verdict == "APPROVED" else "rejected"] += 1

    results: list[dict[str, object]] = []
    for key, counts in sorted(tally.items()):
        ensemble_band, llm_band = key.split("/")
        n, approved = counts["n"], counts["approved"]
        precision = approved / n if n else 0.0
        lower = wilson_lower_bound(approved, n)
        results.append(
            {
                "bucket": key,
                "ensemble_band": ensemble_band,
                "llm_band": llm_band,
                "n_labelled": n,
                "n_approved": approved,
                "n_rejected": counts["rejected"],
                "precision": round(precision, 4),
                "precision_lower_95": round(lower, 4),
                "is_active": 1 if n >= min_labels else 0,
                "reason": (
                    "active"
                    if n >= min_labels
                    else f"only {n} labels (need {min_labels}) -- falls back"
                ),
            }
        )

    if dry_run:
        return results

    for row in results:
        conn.execute(
            sa.text(
                """
                update config.calibration_buckets
                set    n_labelled         = :n_labelled,
                       n_approved         = :n_approved,
                       n_rejected         = :n_rejected,
                       precision_rate     = :precision,
                       precision_lower_95 = :precision_lower_95,
                       min_labels         = :min_labels,
                       is_active          = :is_active,
                       band_high_cutoff   = :band_high,
                       band_mid_cutoff    = :band_mid,
                       source_run_note    = :note,
                       updated_at         = sysutcdatetime(),
                       updated_by         = 'stage_calibration.rebuild'
                where  ensemble_band = :ensemble_band
                and    llm_band      = :llm_band
                """
            ),
            {
                **{k: row[k] for k in (
                    "n_labelled", "n_approved", "n_rejected",
                    "precision", "precision_lower_95", "is_active",
                    "ensemble_band", "llm_band",
                )},
                "min_labels": min_labels,
                "band_high": BAND_HIGH,
                "band_mid": BAND_MID,
                "note": note,
            },
        )
    conn.commit()
    logger.info("Calibration rebuilt: %d bucket(s) updated", len(results))
    return results
