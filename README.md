# sha-pipelines

The SKU-harmonization pipeline: one Prefect flow, nine possible stages
(seven wired in, two stubbed), each stage's logic in its own `stage_*.py`
file. `flow.py` is the one file that imports every piece and declares the
actual flow -- nothing else in this folder knows Prefect exists.

## Files

| File | Role |
|---|---|
| `flow.py` | **The common file.** Imports every `stage_*.py` and `agents/*.py`, wraps each stage in a Prefect `@task`, and declares `run_matching_flow()` as the `@flow`. Each task shows as its own node in the Prefect UI. |
| `checkpoint.py` | Saves/loads each stage's output as plain JSON under `data/intermediate/<run_id>/<stage_name>.json` -- open a file directly to see what a stage produced. |
| `config.py` | All tunable constants -- weights, thresholds, vocab, env-driven DB/LLM config. |
| `models.py` | Typed data shapes (`SourceProduct`, `MasterProduct`, `ScoreBreakdown`, `MatchResult`) -- frozen dataclasses, no behavior. |
| `db.py` | SQL Server 2025 connection + plain parameterized-query helpers. No ORM. |
| `embedder.py` | Local sentence-transformers embeddings with an on-disk cache. |
| `stage_ingest.py` | Stage 0/1 -- load + normalize the raw 1DS/master files. |
| `stage_deterministic.py` | Stage 1 -- approved-crosswalk short-circuit. |
| `stage_lexicon.py` | Stage 2 -- brand gate (done) + governed Lexicon (**stub**, see file). |
| `stage_attributes.py` | Stage 3 -- pack parsing (rules) + LLM fallback hook. |
| `stage_candidates.py` | Stage 4 -- candidate blocking: SQL Server vector search + BM25 + forced-phrase rescue. |
| `stage_scoring.py` | Stage 5 -- gated ensemble scoring. |
| `stage_disposition.py` | Stage 6 -- confidence tiers + banded routing, calls the thin-margin judge. |
| `stage_recovery.py` | Stage 7 -- recovery loop (**stub**, no precedent exists anywhere). |
| `stage_relate.py` | Stage 8 -- relationship discovery / Engine 2 (**stub**, no precedent exists anywhere). |
| `agents/` | LangChain-based LLM touchpoints -- see `agents/README.md`. |

## Running it

Needs a SQL Server instance reachable via `SQL_SERVER_DSN`, and (for
attribute-fallback/thin-margin-judge and embeddings) Azure OpenAI creds --
both read from `sha-pipelines/.env` (gitignored). Defaults to the team's
shared Azure SQL DB (see repo-root README) -- ask a teammate for credentials
if `sha-pipelines/.env` doesn't exist yet. No Docker needed just for the DB.

**With Prefect** (UI, flow-run graph, scheduling):
```bash
cd sha-pipelines
uv sync
uv run prefect server start   # in one terminal -- gives you the UI at localhost:4200
uv run python flow.py         # in another -- runs once, ad hoc
```

Watch the run at `localhost:4200` -- each stage above appears as its own
task node in the flow-run graph, so a failure in (say) `stage_candidates`
is visibly isolated from the rest of the run instead of showing up as one
opaque crash.

To schedule it instead of running ad hoc, see Prefect's `serve()`/deployment
docs -- not wired up yet, deliberately kept out of this pass.

**Without Prefect** (`flow_without_prefect.py` -- same stages, same order,
same checkpoints, plain functions + argparse, zero `prefect` dependency;
for devs who don't want a Prefect server running at all):
```bash
cd sha-pipelines
uv sync
uv run python flow_without_prefect.py match --oneds-file staging.swiggy_products --master-file staging
uv run python flow_without_prefect.py resume <run_id>   # replay persist from a failed run's checkpoint
```

Both entry points share every `stage_*.py`/`agents/*.py` file -- only the
orchestration glue (task/flow decoration vs. plain functions) differs.

Docker works the same way, just containerized -- see the repo root README
and `docker/docker-compose.yml`.

## Inspecting a run between stages

Every stage writes its output to `data/intermediate/<run_id>/<stage_name>.json`
the moment it finishes -- the `run_id` is printed in the flow's logs (and in
the Prefect UI) at the start of the run. Open any of those files directly:

```
data/intermediate/3f9a1c2e-.../stage_ingest__sources.json
data/intermediate/3f9a1c2e-.../stage_ingest__master.json
data/intermediate/3f9a1c2e-.../stage_deterministic__remaining.json
data/intermediate/3f9a1c2e-.../stage_deterministic__resolved.json
data/intermediate/3f9a1c2e-.../stage_lexicon.json
data/intermediate/3f9a1c2e-.../stage_attributes.json
data/intermediate/3f9a1c2e-.../stage_candidates.json
data/intermediate/3f9a1c2e-.../stage_scoring.json
data/intermediate/3f9a1c2e-.../stage_disposition.json
```

This is plain JSON, not a Prefect-internal format -- no Prefect API call or
extra tooling needed to read it. Prefect also has a built-in
`@task(persist_result=True)` feature that does something similar
internally (pickled, not JSON); the two don't conflict if you want both.

## What's real vs. stubbed

Adapted from the working prior art at `Himalaya-SKU-Mapping`: ingest,
deterministic short-circuit, attribute pack-parsing, candidate blocking,
scoring, disposition. Genuinely new, no precedent anywhere: the Lexicon
(`stage_lexicon.py`), the recovery loop (`stage_recovery.py`), and
relationship discovery (`stage_relate.py`) -- these raise
`NotImplementedError` or no-op passthrough rather than fake logic.
`flow.py` does not call `stage_recovery`/`stage_relate` yet for that reason.

## Known simplifications (fix before production)

- **Multipack pack parsing**: `stage_attributes.parse_pack()` only handles
  single-value notation (`50g`, `100 ml`). Multipack notation (`2N X 400G`,
  `700G+400G`) is not handled -- this was flagged as the highest-value fix
  in founding_doc.md section B.4 and deliberately not ported from the prior
  art, which had a live bug here.
- **`stage_ingest.py` uses `df.iterrows()`** -- fine at this scale for a
  first pass, but should be vectorized before running the full ~41k-row
  1DS file.
- **`category_score()` in `stage_scoring.py`** is a placeholder substring
  check, not a real cosine-of-embeddings comparison.
- SQL Server 2025's `VECTOR`/`VECTOR_DISTANCE`/`CREATE VECTOR INDEX` syntax
  is new -- verify against your installed build before running
  `database/schema/004_sqlserver_master_and_ops.sql`.
- `data/intermediate/` will accumulate one folder per run indefinitely --
  add a retention/cleanup policy before running this regularly.
