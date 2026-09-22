"""Record what a run was asked to do, what settings it used, and how it ended.

staging.*_product_mapping only ever holds the LATEST answer for a SKU --
persist_sku_disposition() deletes and re-inserts that SKU's rows every run --
so re-running after an engine fix destroys the result it should be compared
against. These functions write the aggregate that survives, into the tables
sql/015 creates.

Deliberately aggregate-only. It answers "what changed" and never "which SKUs
changed", and it cannot separate +5 net from (+8 gained, -3 lost). Per-SKU
drill-down stays manual against the live mapping table while the result is
current -- a decision taken because a per-SKU snapshot is ~600 KB gzipped per
full-channel run and the aggregate is ~2 KB.

Failure here must never take a run down with it: a run that matched 548 SKUs
correctly but could not write its own history row is still a successful run.
Every function is therefore best-effort and logs rather than raises -- the
one place in this codebase where swallowing an exception is the right call,
because the alternative is losing real work to a bookkeeping fault.
"""
from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone

import sqlalchemy as sa

import common.config as C
from common import db, db_models
from common.scope import ScopeCounts

logger = logging.getLogger(__name__)

# The settings worth pinning to a run. An outcome delta is not interpretable
# without them: "+5 AutoMatch" could be a rules fix, a weight change, or
# different input data. Keys are read off common.config at call time, so a
# new tunable is captured by adding it here and nowhere else.
TRACKED_CONFIG = (
    "TOP_N_OUTPUT",
    "CATEGORY_GATE_MIN_CONF",
    "CATEGORY_GATE_MIN_TARGET_POP",
    "SEMANTIC_TOPK",
    "LEXICAL_TOPK",
    "W_SEMANTIC",
    "W_LEXICAL",
    "W_TYPE",
    "W_PACK",
    "W_OVERLAP",
    "PRODUCT_GROUP_MATCH_BONUS",
    "PACK_TYPE_MISMATCH_PENALTY",
    "DOMAIN_MISMATCH_PENALTY",
    "TYPE_HARD_INCOMPAT_PENALTY",
)

# Defaults as declared in config.py, used only to mark a value as an
# override. Read from the module so the two can never drift.
_DEFAULTS: dict[str, str] = {}


def _git_sha() -> str | None:
    """Short SHA of the working tree. Pins rules.py and the scoring code --
    without it a comparison cannot tell an engine change from a data one."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def start(
    conn: db.Connection,
    execution_id,
    counts: ScopeCounts,
    *,
    workers: int,
    use_llm: bool,
    explicit_sku_count: int | None = None,
    failed_reset: int = 0,
    triggered_by: str | None = None,
    trigger_type: str = "cli",
    config_overrides: dict[str, str] | None = None,
) -> None:
    """Open a run row and snapshot the config it is about to use."""
    try:
        run = db_models.get_table(conn.engine, "audit", "engine_run")
        conn.execute(
            sa.insert(run).values(
                execution_id=str(execution_id),
                channel=counts.channel,
                engine=counts.engine,
                category=counts.category,
                subcategory=counts.subcategory,
                explicit_sku_count=explicit_sku_count,
                scoped_total=counts.scoped_total,
                to_run=counts.to_run,
                approved_skipped=counts.approved_skipped,
                already_mapped=counts.already_mapped,
                failed_reset=failed_reset,
                source_row_count=counts.source_row_count,
                started_at=datetime.now(timezone.utc),
                heartbeat_at=datetime.now(timezone.utc),
                status="running",
                processed=0,
                failed=0,
                triggered_by=triggered_by,
                trigger_type=trigger_type,
                git_sha=_git_sha(),
            )
        )

        cfg = db_models.get_table(conn.engine, "audit", "engine_run_config")
        # The console sets ENGINE_CONFIG_OVERRIDES on the subprocess so the
        # run can record which values were chosen for it rather than
        # inherited from config.py -- the difference a later comparison
        # depends on.
        env_overrides = {
            k for k in os.environ.get("ENGINE_CONFIG_OVERRIDES", "").split(",") if k
        }
        overrides = {**(config_overrides or {}), **{k: "" for k in env_overrides}}
        rows = []
        for key in TRACKED_CONFIG:
            value = getattr(C, key, None)
            if value is None:
                continue
            rows.append({
                "execution_id": str(execution_id),
                "config_key": key,
                "config_value": str(value),
                "is_override": 1 if key in overrides else 0,
            })
        # Not config.py values, but they change the run's shape, so they
        # belong in the same snapshot.
        rows.append({"execution_id": str(execution_id), "config_key": "max_workers",
                     "config_value": str(workers), "is_override": 0})
        rows.append({"execution_id": str(execution_id), "config_key": "use_llm",
                     "config_value": str(bool(use_llm)), "is_override": 0})
        if rows:
            conn.execute(sa.insert(cfg), rows)

        conn.commit()
    except Exception as exc:  # see module docstring
        logger.warning("run_record.start failed (run continues): %s", exc)


def heartbeat(conn: db.Connection, execution_id, processed: int, failed: int) -> None:
    """Mark the run alive. Without this a crashed run stays 'running'
    forever -- 16 rows in pipeline_execution_log have sat that way since
    18 Aug, and every dashboard counts them as active."""
    try:
        run = db_models.get_table(conn.engine, "audit", "engine_run")
        conn.execute(
            sa.update(run)
            .where(run.c.execution_id == str(execution_id))
            .values(heartbeat_at=datetime.now(timezone.utc),
                    processed=processed, failed=failed)
        )
        conn.commit()
    except Exception as exc:
        logger.debug("run_record.heartbeat failed: %s", exc)


def finish(
    conn: db.Connection,
    execution_id,
    *,
    status: str,
    processed: int,
    failed: int,
    outcome: dict[str, int] | None = None,
    error_message: str | None = None,
) -> None:
    """Close the run and record its outcome mix.

    `outcome` is {mapping_status: count} -- the comparison surface. Two runs
    of the same scope joined on status give the before/after delta.
    """
    try:
        run = db_models.get_table(conn.engine, "audit", "engine_run")
        conn.execute(
            sa.update(run)
            .where(run.c.execution_id == str(execution_id))
            .values(
                ended_at=datetime.now(timezone.utc),
                heartbeat_at=datetime.now(timezone.utc),
                status=status,
                processed=processed,
                failed=failed,
                error_message=error_message,
            )
        )
        if outcome:
            tbl = db_models.get_table(conn.engine, "audit", "engine_run_outcome")
            conn.execute(
                sa.delete(tbl).where(tbl.c.execution_id == str(execution_id))
            )
            conn.execute(sa.insert(tbl), [
                {"execution_id": str(execution_id), "status": k, "sku_count": v}
                for k, v in outcome.items() if v
            ])
        conn.commit()
    except Exception as exc:
        logger.warning("run_record.finish failed: %s", exc)


def outcome_for(
    conn: db.Connection, channel: str, engine: str,
    category: str | None, subcategory: str | None,
) -> dict[str, int]:
    """Current status mix for a scope, read straight off the source table.

    Taken after a run rather than counted during it: the source table is the
    authority on where each SKU ended up, and a tally kept in memory would
    drift from it the moment a worker died mid-persist.
    """
    from common.scope import _brand_clause, _not_approved_clause, _source_table

    schema, name = _source_table(channel).split(".", 1)
    source = db_models.get_table(conn.engine, schema, name)

    q = (
        sa.select(source.c.mapping_status, sa.func.count())
        .where(_brand_clause(source, engine))
        .where(_not_approved_clause(source))
        .group_by(source.c.mapping_status)
    )
    if category:
        q = q.where(source.c.category == category)
    if subcategory:
        q = q.where(source.c.subcategory == subcategory)

    return {r[0]: r[1] for r in conn.execute(q).all() if r[0]}


def reconcile_stale(conn: db.Connection, max_age_minutes: int = 30) -> int:
    """Mark runs whose heartbeat stopped as 'stale' rather than 'running'.

    A crashed process cannot close its own row, so without this every such
    run is indistinguishable from a live one. Returns how many were marked.
    """
    try:
        run = db_models.get_table(conn.engine, "audit", "engine_run")
        cutoff = sa.text(f"DATEADD(minute, -{int(max_age_minutes)}, SYSUTCDATETIME())")
        result = conn.execute(
            sa.update(run)
            .where(run.c.status == "running")
            .where(sa.or_(run.c.heartbeat_at.is_(None), run.c.heartbeat_at < cutoff))
            .values(status="stale",
                    error_message="No heartbeat; process presumed dead.")
        )
        conn.commit()
        return result.rowcount
    except Exception as exc:
        logger.warning("run_record.reconcile_stale failed: %s", exc)
        return 0
