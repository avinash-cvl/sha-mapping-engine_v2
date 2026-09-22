"""HTTP surface for the engine console.

The console is one more caller of the same code the CLI calls -- it is not a
replacement for it and holds no logic of its own. Every endpoint here either
reads through common/scope, common/run_record and common/recovery, or shells
out to the very batch_flow module a terminal user would invoke. That is the
whole design: if the two ever disagree about what a scope contains or what a
run did, the console is lying, and a console that lies about a 4-hour job
costing real LLM spend is worse than no console.

A run is therefore a subprocess, not an in-process call. It gives cancellation
a real mechanism, keeps a wedged run from taking the API down with it, and
means the log file on disk is the same artefact either way.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import common.config as C
from common import auth, db, db_models, recovery, run_record, scope

# Applied to the router rather than to each route: a dependency listed here
# cannot be forgotten when someone adds an endpoint later. Every path under
# /api/engine requires an admin session.
router = APIRouter(
    prefix="/api/engine",
    tags=["engine"],
    dependencies=[Depends(auth.require_admin)],
)

CHANNELS = ("amazon", "blinkit", "swiggy", "zepto")
MODULE = {"himalaya": "oneds_master", "competitor": "oneds_competitor"}

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------- models
class ScopeRequest(BaseModel):
    channel: str
    pipeline: str | None = None          # None -> both, as two results
    category: str | None = None
    subcategory: str | None = None
    skus: list[str] | None = None


class RunRequest(ScopeRequest):
    workers: int = Field(default=4, ge=1, le=16)
    use_llm: bool = True
    reset_failed: bool = False           # opt-in; never implicit
    triggered_by: str | None = None
    # Per-run config, handed to the subprocess as environment variables.
    # config.py reads os.environ at import time, so a fresh process picks
    # these up and no other run is affected -- which is why overrides are
    # per-run rather than a global the console mutates.
    overrides: dict[str, str] = Field(default_factory=dict)


# Settings a run may override, with the bounds each is sane within.
# Anything absent here cannot be set from the console at all: TOP_N_OUTPUT is
# the contract with the review portal, and the model deployments would make
# scores incomparable with every prior run.
OVERRIDABLE = {
    "CATEGORY_GATE_MIN_TARGET_POP": (int, 0, 10),
    "CATEGORY_GATE_MIN_CONF": (float, 0.0, 1.1),
    "SEMANTIC_TOPK": (int, 10, 500),
    "LEXICAL_TOPK": (int, 10, 500),
    "W_SEMANTIC": (float, 0.0, 1.0),
    "W_LEXICAL": (float, 0.0, 1.0),
    "W_TYPE": (float, 0.0, 1.0),
    "W_PACK": (float, 0.0, 1.0),
    "W_OVERLAP": (float, 0.0, 1.0),
    "PRODUCT_GROUP_MATCH_BONUS": (float, 1.0, 2.0),
    "PACK_TYPE_MISMATCH_PENALTY": (float, 0.5, 1.0),
}

LOCKED = ("TOP_N_OUTPUT", "AZURE_LLM_DEPLOYMENT", "AZURE_EMBEDDING_DEPLOYMENT")


def _validate_overrides(raw: dict[str, str]) -> dict[str, str]:
    """Reject anything out of range before a run starts.

    A bad weight does not crash the engine -- it quietly produces a different
    ranking, which is far worse than an error. The weights are checked as a
    set too: they are a blend, and one raised in isolation silently changes
    what every other signal is worth.
    """
    clean: dict[str, str] = {}
    for key, value in raw.items():
        if key in LOCKED:
            raise HTTPException(400, f"{key} cannot be overridden.")
        if key not in OVERRIDABLE:
            raise HTTPException(400, f"{key} is not an overridable setting.")
        cast, lo, hi = OVERRIDABLE[key]
        try:
            parsed = cast(value)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key} must be a {cast.__name__}.")
        if not (lo <= parsed <= hi):
            raise HTTPException(400, f"{key} must be between {lo} and {hi}.")
        clean[key] = str(parsed)

    weights = {k: float(v) for k, v in clean.items() if k.startswith("W_")}
    if weights:
        import common.config as cfg
        full = {
            k: weights.get(k, float(getattr(cfg, k)))
            for k in ("W_SEMANTIC", "W_LEXICAL", "W_TYPE", "W_PACK", "W_OVERLAP")
        }
        total = round(sum(full.values()), 4)
        if abs(total - 1.0) > 0.001:
            raise HTTPException(
                400,
                f"Scoring weights must sum to 1.00; these sum to {total:.2f}. "
                "Adjust the others to compensate.",
            )
    return clean


# ------------------------------------------------- in-process run registry
@dataclass
class Run:
    """A launched subprocess and what we know about it.

    Held in memory deliberately: this tracks the PROCESS, which dies with the
    API anyway. The durable record of what a run did lives in
    audit.engine_run, written by the engine itself -- so a restarted API
    loses its handle on a running process but never loses the history.
    """
    run_id: str
    channel: str
    pipeline: str
    category: str | None
    subcategory: str | None
    proc: subprocess.Popen | None = None
    log_path: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    cancelled: bool = False


RUNS: dict[str, Run] = {}




def get_conn():
    """Per-request DB connection, returned to the pool when the request ends.

    db.get_connection() hands out a pooled checkout the caller must close.
    Fifteen endpoints were each opening one and walking away, so the pool
    (5 + 10 overflow) was exhausted after sixteen requests and everything
    after that blocked for thirty seconds and failed. Declaring it as a
    dependency means no endpoint has to remember.
    """
    conn = db.get_connection()
    try:
        yield conn
    finally:
        conn.close()


def _database_name() -> str:
    """Read the target database out of the DSN, so the dialog names the
    database that will actually be written to rather than a label someone
    set once and forgot."""
    dsn = os.environ.get("SQL_SERVER_DSN", "")
    for part in dsn.split(";"):
        if part.strip().lower().startswith("database="):
            return part.split("=", 1)[1].strip()
    return "unknown"


def _database_server() -> str:
    dsn = os.environ.get("SQL_SERVER_DSN", "")
    for part in dsn.split(";"):
        if part.strip().lower().startswith("server="):
            return part.split("=", 1)[1].strip()
    return "unknown"


def _validate(channel: str, pipeline: str | None) -> None:
    if channel not in CHANNELS:
        raise HTTPException(400, f"channel must be one of {CHANNELS}")
    if pipeline is not None and pipeline not in MODULE:
        raise HTTPException(400, f"pipeline must be one of {tuple(MODULE)}")


# ---------------------------------------------------------------- reads
@router.get("/channels")
def channels(conn: db.Connection = Depends(get_conn)) -> dict:
    """Channels, and the categories each actually holds. Drives the scope
    pickers without the client hardcoding a taxonomy that changes on ingest."""
    out = {}
    for ch in CHANNELS:
        src = db_models.get_table(conn.engine, "staging", f"{ch}_products")
        rows = conn.execute(
            sa.select(src.c.category, src.c.subcategory, sa.func.count())
            .group_by(src.c.category, src.c.subcategory)
            .order_by(src.c.category, src.c.subcategory)
        ).all()
        cats: dict[str, list[str]] = {}
        for cat, sub, _ in rows:
            if cat:
                cats.setdefault(cat, []).append(sub)
        out[ch] = cats
    return out


@router.post("/scope")
def resolve_scope(req: ScopeRequest, conn: db.Connection = Depends(get_conn)) -> dict:
    """Count what a run WOULD process, before anything is launched.

    Returns a breakdown, never one figure. The distinction is not cosmetic:
    a single "552 rows" total hid a 2x duplicate ingest for weeks, where
    274 to-run / 88 approved-skipped / 0 already-mapped shows the shape.
    """
    _validate(req.channel, req.pipeline)
    pipelines = [req.pipeline] if req.pipeline else list(MODULE)
    legs = [
        scope.resolve(conn, req.channel, p, req.category, req.subcategory, req.skus).as_dict()
        for p in pipelines
    ]
    total_to_run = sum(x["to_run"] for x in legs)
    return {
        "legs": legs,
        # Estimates, and labelled as such. 3 LLM calls per SKU and ~0.34
        # SKU/s are measured from prior runs, not guessed -- but they are
        # still projections, so the UI must not present them as fact.
        "estimate": {
            "llm_calls": total_to_run * 3,
            "seconds": int(total_to_run / 0.34) if total_to_run else 0,
            "basis": "3 LLM calls/SKU and 0.34 SKU/s, measured over prior runs",
        },
    }


@router.get("/runs")
def list_runs(limit: int = Query(50, le=500), channel: str | None = None, conn: db.Connection = Depends(get_conn)) -> list[dict]:
    """Run history. reconcile_stale() runs first so a crashed run is reported
    as stale rather than sitting in 'running' forever -- 16 rows in the old
    pipeline_execution_log have done exactly that since 18 August."""
    run_record.reconcile_stale(conn)

    run = db_models.get_table(conn.engine, "audit", "engine_run")
    q = sa.select(run).order_by(run.c.started_at.desc()).limit(limit)
    if channel:
        q = q.where(run.c.channel == channel)
    return [dict(r._mapping) for r in conn.execute(q).all()]


@router.post("/runs/reconcile")
def reconcile(
    max_age_minutes: int = Query(30, ge=1, le=1440),
    user: auth.User = Depends(auth.current_user),
    conn: db.Connection = Depends(get_conn),
) -> dict:
    """Mark runs whose heartbeat stopped as stale.

    A crashed process cannot close its own row, so without this every such run
    is indistinguishable from a live one -- 16 have sat in 'running' since
    18 August, and any "currently running" count includes them.
    """
    return {"reconciled": run_record.reconcile_stale(conn, max_age_minutes)}


@router.get("/runs/{run_id}")
def get_run(run_id: str, conn: db.Connection = Depends(get_conn)) -> dict:
    run = db_models.get_table(conn.engine, "audit", "engine_run")
    row = conn.execute(sa.select(run).where(run.c.execution_id == run_id)).first()
    if row is None:
        raise HTTPException(404, "run not found")

    oc = db_models.get_table(conn.engine, "audit", "engine_run_outcome")
    cfg = db_models.get_table(conn.engine, "audit", "engine_run_config")
    return {
        "run": dict(row._mapping),
        "outcome": {
            r.status: r.sku_count
            for r in conn.execute(sa.select(oc).where(oc.c.execution_id == run_id)).all()
        },
        "config": {
            r.config_key: {"value": r.config_value, "is_override": bool(r.is_override)}
            for r in conn.execute(sa.select(cfg).where(cfg.c.execution_id == run_id)).all()
        },
        "live": run_id in RUNS and RUNS[run_id].proc is not None
                and RUNS[run_id].proc.poll() is None,
    }


@router.get("/compare")
def compare(before: str, after: str, conn: db.Connection = Depends(get_conn)) -> dict:
    """Aggregate before/after between two runs.

    Aggregate only, and the response says so. It cannot distinguish +5 net
    from (+8 gained, -3 lost), and the flags matter as much as the deltas: a
    change in source_row_count or git_sha means the delta may not be engine
    behaviour at all.
    """
    rows = conn.execute(
        sa.text("""
            SELECT status, before_count, after_count, delta,
                   input_population_changed, code_changed
            FROM audit.vw_engine_run_compare
            WHERE before_run = :b AND after_run = :a
            ORDER BY ABS(delta) DESC
        """),
        {"b": before, "a": after},
    ).all()
    if not rows:
        raise HTTPException(404, "no comparable outcome rows for that pair")

    run = db_models.get_table(conn.engine, "audit", "engine_run")
    meta = {
        rid: dict(conn.execute(sa.select(run).where(run.c.execution_id == rid)).first()._mapping)
        for rid in (before, after)
    }
    return {
        "before": meta[before],
        "after": meta[after],
        "rows": [
            {"status": r[0], "before": r[1], "after": r[2], "delta": r[3]}
            for r in rows
        ],
        "flags": {
            "input_population_changed": bool(rows[0][4]),
            "code_changed": bool(rows[0][5]),
        },
        "note": (
            "Aggregate only: shows how many SKUs landed in each outcome, not "
            "which ones moved. A net +5 could be five gained, or eight gained "
            "and three lost."
        ),
    }


# ---------------------------------------------------------------- recovery
@router.post("/failed/preview")
def failed_preview(req: ScopeRequest, conn: db.Connection = Depends(get_conn)) -> dict:
    _validate(req.channel, req.pipeline)
    return recovery.preview(
        conn, req.channel, req.pipeline, req.category, req.subcategory, req.skus
    ).as_dict()


@router.post("/failed/reset")
def failed_reset(
    req: ScopeRequest,
    user: auth.User = Depends(auth.current_user),
    conn: db.Connection = Depends(get_conn),
) -> dict:
    """Flip Failed rows back to PENDING so the next run picks them up.

    Explicit by design. A deadlocked worker leaves its row 'Failed', step 6
    selects only 'PENDING', and so a plain re-run skips it silently. The
    engine keeps that narrow contract -- 'Failed' also covers genuine
    repeatable errors -- and whether to retry is a decision somebody makes.
    Steward-approved rows are never touched, and that is not overridable.
    """
    _validate(req.channel, req.pipeline)
    conn = db.get_connection()
    return recovery.reset_failed(
        conn, req.channel, req.pipeline, req.category, req.subcategory,
        req.skus, actor=user.email,
    ).as_dict()


# ---------------------------------------------------------------- launch
def _build_command(req: RunRequest, pipeline: str, run_id: str, log_path: str) -> list[str]:
    """The argv the subprocess will receive.

    Shared by the confirmation preview and the launch itself, deliberately:
    a modal that shows a command assembled by different code eventually shows
    a command that is not the one that runs, and the whole point of asking
    someone to review it is that what they read is what executes.
    """
    cmd = [
        sys.executable, "-m", f"{MODULE[pipeline]}.batch_flow", "match",
        "--source-table", f"staging.{req.channel}_products",
        "--master-table", "staging.himalaya_products",
        "--max-workers", str(req.workers),
        "--log-file", log_path,
        # The engine must adopt this id, not mint its own: the console
        # streams progress against it from the moment the subprocess starts.
        "--run-id", run_id,
    ]
    if req.category:
        cmd += ["--category", req.category]
    if req.subcategory:
        cmd += ["--subcategory", req.subcategory]
    if req.skus:
        cmd += ["--sku", *req.skus]
    if not req.use_llm:
        cmd.append("--no-llm")
    return cmd


def _shell_quote(cmd: list[str]) -> str:
    """Render argv as a copy-pasteable line, quoting only what needs it."""
    out = []
    for part in cmd:
        out.append(f'"{part}"' if (" " in part or "&" in part) else part)
    return " ".join(out)


def _spawn(req: RunRequest, pipeline: str, overrides: dict[str, str]) -> Run:
    run_id = str(uuid.uuid4())
    log_path = os.path.join(REPO_ROOT, "logs", f"console_{run_id}.log")

    cmd = _build_command(req, pipeline, run_id, log_path)

    env = {
        **os.environ,
        **overrides,
        "ENGINE_TRIGGERED_BY": req.triggered_by or "console",
        "ENGINE_TRIGGER_TYPE": "manual",
        # Recorded against the run so audit.engine_run_config can mark which
        # values were overridden rather than inherited.
        "ENGINE_CONFIG_OVERRIDES": ",".join(sorted(overrides)),
    }
    proc = subprocess.Popen(
        cmd, cwd=REPO_ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    r = Run(run_id=run_id, channel=req.channel, pipeline=pipeline,
            category=req.category, subcategory=req.subcategory,
            proc=proc, log_path=log_path)
    RUNS[run_id] = r
    return r


@router.get("/results")
def results(
    channel: str,
    category: str | None = None,
    subcategory: str | None = None,
    pipeline: str | None = None,
    status: str | None = None,
    q: str | None = None,
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
) -> dict:
    """Rank-1 match per SKU, filtered.

    Server-side throughout: amazon competitor alone is 151k rows, so a client
    that fetched everything and filtered in the browser would be unusable on
    the one channel that matters most.

    Category and sub-category come back per row because a run spans one
    category but many sub-category groups -- 14 of them in a single 51-SKU
    run -- so without them a row cannot be placed.
    """
    _validate(channel, pipeline)
    conn = db.get_connection()

    src = db_models.get_table(conn.engine, "staging", f"{channel}_products")
    mapping = db_models.get_table(conn.engine, "staging", f"{channel}_product_mapping")

    join = sa.join(src, mapping, mapping.c.source_sku == src.c.sku)
    where = [mapping.c.match_rank == 1]
    if category:
        where.append(src.c.category == category)
    if subcategory:
        where.append(src.c.subcategory == subcategory)
    if pipeline:
        where.append(scope._brand_clause(src, pipeline))
    if status:
        where.append(src.c.mapping_status == status)
    if q:
        like = f"%{q}%"
        where.append(
            sa.or_(
                src.c.sku.like(like),
                src.c.title.like(like),
                mapping.c.product_code.like(like),
                mapping.c.product_name.like(like),
            )
        )

    total = conn.execute(
        sa.select(sa.func.count()).select_from(join).where(*where)
    ).scalar() or 0

    rows = conn.execute(
        sa.select(
            src.c.sku, src.c.title, src.c.brand, src.c.category, src.c.subcategory,
            src.c.mapping_status, src.c.pack_size, src.c.uom,
            mapping.c.product_code, mapping.c.product_name, mapping.c.final_score,
            mapping.c.confidence_level, mapping.c.llm_reasoning,
        )
        .select_from(join)
        .where(*where)
        .order_by(src.c.category, src.c.subcategory, src.c.sku)
        .offset(offset)
        .limit(limit)
    ).mappings().all()

    himalaya = [b.lower() for b in C.HIMALAYA_BRANDS]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "rows": [
            {
                "sku": r["sku"],
                "title": r["title"],
                "brand": r["brand"],
                "category": r["category"],
                "subcategory": r["subcategory"],
                "status": r["mapping_status"],
                "pack": (
                    f'{r["pack_size"]}{r["uom"]}'
                    if r["pack_size"] is not None else None
                ),
                "product_code": r["product_code"],
                "product_name": r["product_name"],
                "score": float(r["final_score"]) if r["final_score"] is not None else None,
                "confidence": r["confidence_level"],
                "reasoning": r["llm_reasoning"],
                "pipeline": (
                    "himalaya"
                    if (r["brand"] or "").strip().lower() in himalaya
                    else "competitor"
                ),
            }
            for r in rows
        ],
    }


@router.get("/results/groups")
def result_groups(
    channel: str,
    category: str | None = None,
    pipeline: str | None = None,
    conn: db.Connection = Depends(get_conn),
) -> list[dict]:
    """Outcome mix per (category, sub-category).

    The unit worth reporting against: AutoMatch rate ranged 78-100% across
    categories in one run, and that spread is the finding -- it says where the
    engine is weak. No amount of scrolling a flat row list surfaces it.
    """
    _validate(channel, pipeline)
    conn = db.get_connection()
    src = db_models.get_table(conn.engine, "staging", f"{channel}_products")

    statuses = ("AutoMatch", "StewardReview", "LowConfidence",
                "NoHimalayaEquivalent", "Failed", "PENDING")

    q = (
        sa.select(
            src.c.category,
            src.c.subcategory,
            sa.func.count().label("total"),
            *[
                sa.func.sum(sa.case((src.c.mapping_status == st, 1), else_=0)).label(st)
                for st in statuses
            ],
        )
        .where(src.c.mapping_status.isnot(None))
        .group_by(src.c.category, src.c.subcategory)
        .order_by(sa.func.count().desc())
    )
    if category:
        q = q.where(src.c.category == category)
    if pipeline:
        q = q.where(scope._brand_clause(src, pipeline))

    out = []
    for r in conn.execute(q).all():
        counts = {st: (r[3 + i] or 0) for i, st in enumerate(statuses)}
        decided = sum(
            counts[st] for st in
            ("AutoMatch", "StewardReview", "LowConfidence", "NoHimalayaEquivalent")
        )
        out.append({
            "category": r[0],
            "subcategory": r[1],
            "total": r[2],
            **counts,
            # Share of DECIDED rows, not of everything: counting PENDING in
            # the denominator makes a half-finished category look weak when
            # it is merely unfinished.
            "automatch_rate": (
                round(counts["AutoMatch"] / decided, 3) if decided else None
            ),
        })
    return out


@router.get("/gate-warnings")
def gate_warnings(limit: int = Query(50, le=200), conn: db.Connection = Depends(get_conn)) -> list[dict]:
    """Master nodes rules.py can target that hold too few rows to be a
    shortlist.

    A gate onto a node holding one product returns that product whatever the
    listing says -- which is how every serum listing came back as the same
    SERUM FLUID. 86 of 191 targets hold three rows or fewer, so this is a
    standing queue rather than a one-off audit.
    """
    master = db_models.get_table(conn.engine, "staging", "himalaya_products")

    pop = {
        ((r[0] or "").strip().lower(), (r[1] or "").strip().lower()): r[2]
        for r in conn.execute(
            sa.select(
                master.c.normalized_category,
                master.c.normalized_subcategory,
                sa.func.count(),
            )
            .where(master.c.is_active == True)  # noqa: E712 - SQL, not Python
            .group_by(master.c.normalized_category, master.c.normalized_subcategory)
        ).all()
    }

    from oneds_master.category.resolve import pack_targets
    from oneds_master.category.rules import PACKS

    seen: dict[tuple[str, str], set[str]] = {}
    for name, pack in PACKS.items():
        for cat, sub in pack_targets(pack):
            seen.setdefault(
                (cat.strip().lower(), sub.strip().lower()), set()
            ).add(name)

    rows = []
    for key, packs in seen.items():
        n = pop.get(key, 0)
        if n < C.CATEGORY_GATE_MIN_TARGET_POP:
            rows.append({
                "node": f"{key[0].upper()} / {key[1].upper()}",
                "rows": n,
                "reached_from": sorted(packs),
                "state": "does not exist" if n == 0 else "starved",
            })
    rows.sort(key=lambda r: (r["rows"], r["node"]))
    return rows[:limit]


@router.post("/plan")
def plan(req: RunRequest, user: auth.User = Depends(auth.current_user), conn: db.Connection = Depends(get_conn)) -> dict:
    """Everything the confirmation dialog must show, resolved server-side.

    A run can rewrite mappings for six figures of SKUs and spend real money
    doing it, so the dialog is not decoration -- it is the last point at which
    someone can notice that the scope is not what they meant. Every field here
    is therefore read from the same code the run uses, not reconstructed in
    the browser from form state: a dialog that describes a different run than
    the one that executes is worse than no dialog, because it converts a
    careful reader into a confident one.

    Nothing is launched. This is a read.
    """
    _validate(req.channel, req.pipeline)
    overrides = _validate_overrides(req.overrides)   # fail here, not after the click

    pipelines = [req.pipeline] if req.pipeline else list(MODULE)
    legs, commands = [], []

    for pipe in pipelines:
        counts = scope.resolve(
            conn, req.channel, pipe, req.category, req.subcategory, req.skus
        ).as_dict()
        counts["module"] = MODULE[pipe]
        counts["brand_filter"] = (
            f"brand IN {tuple(C.HIMALAYA_BRANDS)}" if pipe == "himalaya"
            else f"brand NOT IN {tuple(C.HIMALAYA_BRANDS)}"
        )
        legs.append(counts)
        commands.append({
            "pipeline": pipe,
            # <run-id> is a placeholder: the real id is minted at launch and
            # returned then. Showing a fake uuid here would be a lie about
            # something the user is being asked to verify.
            "command": _shell_quote(
                _build_command(req, pipe, "<run-id>", "logs/console_<run-id>.log")
            ),
        })

    total = sum(l["to_run"] for l in legs)

    # Conflicts are reported rather than raised: the dialog should say why
    # the button is disabled, not fail after the user commits.
    blocked = [
        {"pipeline": r.pipeline, "run_id": r.run_id}
        for r in RUNS.values()
        if r.proc and r.proc.poll() is None
        and r.channel == req.channel and r.category == req.category
        and r.pipeline in pipelines
    ]

    return {
        "environment": {
            "label": os.environ.get("ENGINE_ENV", "LOCAL"),
            "database": _database_name(),
            "server": _database_server(),
        },
        "scope": {
            "channel": req.channel,
            "category": req.category or "ALL categories",
            "subcategory": req.subcategory or "ALL sub-categories",
            "explicit_skus": len(req.skus) if req.skus else None,
        },
        "legs": legs,
        "totals": {
            "to_run": total,
            "approved_skipped": sum(l["approved_skipped"] for l in legs),
            "already_mapped": sum(l["already_mapped"] for l in legs),
            "failed_resettable": sum(l["failed_resettable"] for l in legs),
            "llm_calls": total * 3,
            "seconds": int(total / 0.34) if total else 0,
        },
        "settings": {
            "use_llm": req.use_llm,
            "workers": req.workers,
            "reset_failed_first": req.reset_failed,
            "overrides": overrides,
            "top_n_output": C.TOP_N_OUTPUT,
        },
        "rules": [
            "Steward-approved SKUs are skipped. This cannot be overridden here.",
            "Only PENDING rows are processed; already-mapped rows are left alone.",
            "Rows left 'Failed' by a dead worker are NOT picked up unless reset first.",
            "Each SKU's existing mapping rows are deleted and rewritten when it is reprocessed.",
        ],
        "commands": commands,
        "blocked_by": blocked,
        "triggered_by": user.email,
    }


@router.post("/runs")
def launch(req: RunRequest, user: auth.User = Depends(auth.current_user), conn: db.Connection = Depends(get_conn)) -> dict:
    """Launch one run per requested pipeline.

    "Both" is two runs, never one: the pipelines apply opposite brand filters
    and cannot share a batch. They are launched together rather than queued --
    their row sets are disjoint, and 120 pairs of runs in the existing history
    already overlapped in time without incident. What is NOT safe is unbounded
    concurrency on one table: deadlocks appeared at six workers in a single
    run, so the same scope is refused while it is already running.
    """
    _validate(req.channel, req.pipeline)
    pipelines = [req.pipeline] if req.pipeline else list(MODULE)

    for p in pipelines:
        for existing in RUNS.values():
            if (existing.proc and existing.proc.poll() is None
                    and existing.channel == req.channel
                    and existing.pipeline == p
                    and existing.category == req.category):
                raise HTTPException(
                    409,
                    f"a {p} run for {req.channel}/{req.category or 'all'} is "
                    f"already in progress ({existing.run_id})",
                )

    overrides = _validate_overrides(req.overrides)


    # Attribution comes from the session, never from the request body. A
    # client-supplied name in an audit trail is a suggestion, not a record.
    req = req.model_copy(update={"triggered_by": user.email})

    reset = {}
    if req.reset_failed:
        for p in pipelines:
            reset[p] = recovery.reset_failed(
                conn, req.channel, p, req.category, req.subcategory,
                actor=user.email,
            ).rows_reset

    launched = [_spawn(req, p, overrides) for p in pipelines]
    return {
        "runs": [
            {"run_id": r.run_id, "pipeline": r.pipeline, "log": r.log_path}
            for r in launched
        ],
        "failed_rows_reset": reset,
        "parallel": len(launched) > 1,
        "overrides": overrides,
    }


@router.post("/runs/{run_id}/cancel")
def cancel(run_id: str) -> dict:
    r = RUNS.get(run_id)
    if r is None or r.proc is None:
        raise HTTPException(404, "no live process for that run")
    if r.proc.poll() is not None:
        return {"run_id": run_id, "already_finished": True}
    r.cancelled = True
    r.proc.terminate()
    return {"run_id": run_id, "cancelled": True}


# ---------------------------------------------------------------- progress
@router.get("/runs/{run_id}/stream")
async def stream(run_id: str) -> EventSourceResponse:
    """Progress for one run, as server-sent events.

    Polls audit.engine_run's heartbeat rather than parsing the log: the row is
    what the engine actually committed, and it stays correct across an API
    restart or a client reconnect. Emitted about once a second -- a per-SKU
    event stream would flood the client on a 5,000-SKU run for no extra
    information.
    """
    # Not the per-request dependency: this generator outlives the request
    # handler by design, polling for as long as the run lasts. It therefore
    # owns its checkout and closes it in the generator's finally, or a
    # long-running stream would hold a pooled connection with nobody to
    # return it.
    conn = db.get_connection()
    run = db_models.get_table(conn.engine, "audit", "engine_run")

    async def events():
        last = None
        # A run row appears only once the subprocess has started and written
        # it. Wait briefly rather than declaring the run unknown on the first
        # poll -- the console opens this stream the instant it launches.
        waited = 0.0
        while True:
            row = conn.execute(
                sa.select(run).where(run.c.execution_id == run_id)
            ).first()

            if row is None:
                if waited < 30.0:
                    waited += 1.0
                    yield {"event": "waiting",
                           "data": json.dumps({"run_id": run_id, "status": "starting"})}
                    await asyncio.sleep(1.0)
                    continue
                # Give up, and end the stream in a way the browser will not
                # retry. EventSource reconnects on any dropped connection, so
                # a stream that simply stops re-requests forever -- which is
                # what filled the server log with the same GET. `done` tells
                # the client to close it.
                yield {"event": "done", "data": json.dumps(
                    {"run_id": run_id, "status": "unknown",
                     "error": "No run with that id was recorded."})}
                return

            payload = {
                "run_id": run_id,
                "status": row.status,
                "processed": row.processed,
                "failed": row.failed,
                "to_run": row.to_run,
                "heartbeat_at": row.heartbeat_at.isoformat() if row.heartbeat_at else None,
            }
            if payload != last:
                yield {"event": "progress", "data": json.dumps(payload)}
                last = payload

            if row.status not in ("running",):
                yield {"event": "done", "data": json.dumps(payload)}
                return

            await asyncio.sleep(1.0)

    async def guarded():
        try:
            async for event in events():
                yield event
        finally:
            conn.close()

    return EventSourceResponse(guarded())


@router.get("/runs/{run_id}/log")
def tail_log(run_id: str, lines: int = Query(200, le=5000)) -> dict:
    """Tail of a run's log. Streamed through the API rather than exposing a
    path, so the client never addresses the filesystem directly."""
    r = RUNS.get(run_id)
    path = r.log_path if r else None
    if not path or not os.path.exists(path):
        # A CLI run names its own file; find it rather than 404 on a run the
        # console did not launch.
        candidates = [
            os.path.join(REPO_ROOT, "logs", f)
            for f in os.listdir(os.path.join(REPO_ROOT, "logs"))
            if run_id.lower() in f.lower()
        ]
        path = candidates[0] if candidates else None
    if not path or not os.path.exists(path):
        raise HTTPException(404, "no log file for that run")

    with open(path, encoding="utf-8", errors="replace") as fh:
        tail = fh.readlines()[-lines:]
    return {"run_id": run_id, "path": os.path.basename(path), "lines": tail}


# ---------------------------------------------------------------- config
@router.get("/config")
def get_config() -> dict:
    """The tunables as this process sees them, grouped by what a change to
    them risks.

    Read-only for now, and honest about why: config.py is read at import
    time, so a value written here would not reach a subprocess launched
    later without an env-var handoff or a restart. Showing the live values
    with their provenance is useful on its own -- config drift is otherwise
    invisible -- and is a smaller thing to get right than editing.
    """
    import common.config as cfg

    def item(key, group, blurb, locked=False):
        return {
            "key": key,
            "value": str(getattr(cfg, key, "")),
            "group": group,
            "description": blurb,
            "locked": locked,
            "env_override": key in os.environ,
        }

    return {
        "settings": [
            item("TOP_N_OUTPUT", "Guardrails",
                 "Candidates handed to the LLM judge. Fixed at 3 -- the contract "
                 "with the steward review portal, which renders exactly three.",
                 locked=True),
            item("CATEGORY_GATE_MIN_TARGET_POP", "Guardrails",
                 "Decline to gate onto a master node holding fewer products than "
                 "this. 0 disables the backstop; 86 of 191 gate targets hold 3 rows "
                 "or fewer."),
            item("CATEGORY_GATE_MIN_CONF", "Guardrails",
                 "Minimum resolver confidence before the category gate is trusted "
                 "to narrow the pool. 1.1 disables gating entirely."),
            item("SEMANTIC_TOPK", "Retrieval",
                 "Vector search depth. A candidate never retrieved is scored as "
                 "dissimilar."),
            item("LEXICAL_TOPK", "Retrieval", "BM25 search depth."),
            item("W_SEMANTIC", "Scoring weights", "Embedding similarity."),
            item("W_TYPE", "Scoring weights",
                 "Product-type alignment -- a shampoo must not match a conditioner."),
            item("W_LEXICAL", "Scoring weights", "BM25 token overlap."),
            item("W_OVERLAP", "Scoring weights", "Shared-keyword bonus."),
            item("W_PACK", "Scoring weights", "Pack-size agreement."),
            item("PRODUCT_GROUP_MATCH_BONUS", "Scoring weights",
                 "Applied when the source title names the master's product group."),
            item("PACK_TYPE_MISMATCH_PENALTY", "Scoring weights",
                 "Mild by design -- pack type is weak evidence."),
        ],
        "llm": {
            "deployment": os.environ.get("AZURE_LLM_DEPLOYMENT", ""),
            "embedding": os.environ.get("AZURE_EMBEDDING_DEPLOYMENT", ""),
        },
        # Surfaced as a set, with the total, because they are a blend: one
        # raised in isolation silently changes what every other signal is
        # worth, and a page listing five unrelated numbers invites exactly
        # that change.
        "weights": {
            "values": {
                k: float(getattr(cfg, k))
                for k in ("W_SEMANTIC", "W_LEXICAL", "W_TYPE", "W_PACK", "W_OVERLAP")
            },
            "total": round(sum(
                float(getattr(cfg, k))
                for k in ("W_SEMANTIC", "W_LEXICAL", "W_TYPE", "W_PACK", "W_OVERLAP")
            ), 4),
            "note": (
                "Measured on 809 mapped SKUs, every re-weighting tried traded about "
                "five recovered matches for 12-35 broken accepted mappings. Run a "
                "regression before trusting a change here."
            ),
        },
        "note": (
            "Read-only. config.py is read at import time, so an edit here would "
            "not reach a run launched afterwards without a restart. Override per "
            "run with an environment variable, and use the Run page's Advanced "
            "panel for workers and the LLM toggle."
        ),
    }


@router.get("/config/history")
def config_history(limit: int = Query(30, le=200), conn: db.Connection = Depends(get_conn)) -> list[dict]:
    """Which settings each recent run actually used.

    This is the answer to config drift: not what config.py says today, but
    what produced a given result. A delta between two runs is only
    interpretable alongside it.
    """
    rows = conn.execute(
        sa.text("""
            SELECT TOP (:n) r.execution_id, r.started_at, r.channel, r.pipeline,
                   r.git_sha, c.config_key, c.config_value, c.is_override
            FROM audit.engine_run r
            JOIN audit.engine_run_config c ON c.execution_id = r.execution_id
            ORDER BY r.started_at DESC
        """),
        {"n": limit * 20},
    ).all()

    runs: dict[str, dict] = {}
    for r in rows:
        entry = runs.setdefault(str(r[0]), {
            "execution_id": str(r[0]), "started_at": r[1],
            "channel": r[2], "pipeline": r[3], "git_sha": r[4], "config": {},
        })
        entry["config"][r[5]] = {"value": r[6], "is_override": bool(r[7])}
    return list(runs.values())[:limit]
