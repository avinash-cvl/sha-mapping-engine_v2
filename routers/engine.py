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


def _validate(channel: str, pipeline: str | None) -> None:
    if channel not in CHANNELS:
        raise HTTPException(400, f"channel must be one of {CHANNELS}")
    if pipeline is not None and pipeline not in MODULE:
        raise HTTPException(400, f"pipeline must be one of {tuple(MODULE)}")


# ---------------------------------------------------------------- reads
@router.get("/channels")
def channels() -> dict:
    """Channels, and the categories each actually holds. Drives the scope
    pickers without the client hardcoding a taxonomy that changes on ingest."""
    conn = db.get_connection()
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
def resolve_scope(req: ScopeRequest) -> dict:
    """Count what a run WOULD process, before anything is launched.

    Returns a breakdown, never one figure. The distinction is not cosmetic:
    a single "552 rows" total hid a 2x duplicate ingest for weeks, where
    274 to-run / 88 approved-skipped / 0 already-mapped shows the shape.
    """
    _validate(req.channel, req.pipeline)
    conn = db.get_connection()
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
def list_runs(limit: int = Query(50, le=500), channel: str | None = None) -> list[dict]:
    """Run history. reconcile_stale() runs first so a crashed run is reported
    as stale rather than sitting in 'running' forever -- 16 rows in the old
    pipeline_execution_log have done exactly that since 18 August."""
    conn = db.get_connection()
    run_record.reconcile_stale(conn)

    run = db_models.get_table(conn.engine, "audit", "engine_run")
    q = sa.select(run).order_by(run.c.started_at.desc()).limit(limit)
    if channel:
        q = q.where(run.c.channel == channel)
    return [dict(r._mapping) for r in conn.execute(q).all()]


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    conn = db.get_connection()
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
def compare(before: str, after: str) -> dict:
    """Aggregate before/after between two runs.

    Aggregate only, and the response says so. It cannot distinguish +5 net
    from (+8 gained, -3 lost), and the flags matter as much as the deltas: a
    change in source_row_count or git_sha means the delta may not be engine
    behaviour at all.
    """
    conn = db.get_connection()
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
def failed_preview(req: ScopeRequest) -> dict:
    _validate(req.channel, req.pipeline)
    conn = db.get_connection()
    return recovery.preview(
        conn, req.channel, req.pipeline, req.category, req.subcategory, req.skus
    ).as_dict()


@router.post("/failed/reset")
def failed_reset(
    req: ScopeRequest, user: auth.User = Depends(auth.current_user)
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
def _spawn(req: RunRequest, pipeline: str, overrides: dict[str, str]) -> Run:
    run_id = str(uuid.uuid4())
    log_path = os.path.join(REPO_ROOT, "logs", f"console_{run_id}.log")

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


@router.post("/runs")
def launch(req: RunRequest, user: auth.User = Depends(auth.current_user)) -> dict:
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

    conn = db.get_connection()

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

    return EventSourceResponse(events())


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
        "note": (
            "Read-only. config.py is read at import time, so an edit here would "
            "not reach a run launched afterwards without a restart. Override per "
            "run with an environment variable, and use the Run page's Advanced "
            "panel for workers and the LLM toggle."
        ),
    }


@router.get("/config/history")
def config_history(limit: int = Query(30, le=200)) -> list[dict]:
    """Which settings each recent run actually used.

    This is the answer to config drift: not what config.py says today, but
    what produced a given result. A delta between two runs is only
    interpretable alongside it.
    """
    conn = db.get_connection()
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
