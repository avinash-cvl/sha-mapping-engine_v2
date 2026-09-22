# Database changes — tracking record

One row per schema change, so a prod deployment can be reconciled against
what was applied in dev. Newest first.

---

## 015 — Engine run history

**File:** `sql/015_engine_run_history.sql`
**Applied to dev:** 2026-09-22 (`AureusSentinelv3_staging` on localhost)
**Applied to prod:** _not yet_
**Branch:** `feature/engine-console`

### What it adds

| Object | Type | Purpose |
|---|---|---|
| `audit.engine_run` | table | One row per run: scope, resolved counts, who, when, status |
| `audit.engine_run_config` | table | The tunables that run used (key/value) |
| `audit.engine_run_outcome` | table | Status → SKU count, the comparison surface |
| `audit.vw_engine_run_compare` | view | Before/after delta between two runs, computed on read |

### Risk

**Additive only.** No existing table, column, index or constraint is
modified or dropped. No data is migrated. Nothing outside these four objects
is touched, and no existing query plan changes.

The engine does not yet write to them — as of this commit nothing calls
`common/run_record.py`, so applying the migration changes no behaviour at
all. It is safe to apply ahead of the code.

### Sizing

Roughly 2 KB per run: one `engine_run` row, ~16 `engine_run_config` rows,
and one `engine_run_outcome` row per distinct status. At the current rate
(415 runs to date) the three tables together stay under 1 MB indefinitely.

Deliberately no per-SKU rows. That is the trade that keeps it small: the
aggregate answers *what* changed but never *which SKUs* changed, and cannot
separate +5 net from (+8 gained, −3 lost). Per-SKU drill-down is done by hand
against `staging.*_product_mapping` while the result is still live.

### Dependencies

Needs the `audit` schema, which already exists. Creates it if absent.

`ON DELETE CASCADE` from both child tables to `audit.engine_run`, so
deleting a run row cleans up its config and outcome rows.

### Numbering

`012`–`014` are taken on branch `fix/mapping-quality-TP-FP` (identity
scoring). This migration is `015` to avoid a collision if that branch merges.

### Rollback

```sql
DROP VIEW  IF EXISTS audit.vw_engine_run_compare;
DROP TABLE IF EXISTS audit.engine_run_outcome;
DROP TABLE IF EXISTS audit.engine_run_config;
DROP TABLE IF EXISTS audit.engine_run;
```

Safe at any time — nothing else references these objects. Dropping them
loses run history only; no operational data is affected.

### How to apply

The file is GO-batched, so it needs a client that splits on `GO`
(sqlcmd, SSMS, Azure Data Studio). Applying it through a plain
single-statement driver will fail on the second batch.

Verify afterwards — "the batches ran" is not evidence the objects exist:

```sql
SELECT s.name + '.' + t.name
FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.name LIKE 'engine_run%';
-- expect 3 rows

SELECT COUNT(*) FROM sys.views v
JOIN sys.schemas s ON s.schema_id = v.schema_id
WHERE s.name = 'audit' AND v.name = 'vw_engine_run_compare';
-- expect 1
```

---

## Note on `audit.activity_log`

Not a change — a constraint worth knowing about before writing to it.

`audit.activity_log.input` carries `CHECK (isjson([input]) = 1)`, so the
column takes a JSON document, not prose. `common/recovery.py` writes
`FAILED_ROWS_RESET` entries in the same shape `STAGING_REVIEW_UPDATED` and
`CROSSWALK_UPSERT` already use. A plain-text insert is rejected at the
constraint.

---

## Console authentication — no schema change

The console authenticates against the existing `config.users` (25 accounts,
argon2id hashes, `role` column). **No table is created, altered or written
to.** Reads only, and only on sign-in.

Two environment variables are required in prod:

| Variable | Purpose |
|---|---|
| `CONSOLE_JWT_SECRET` | Signs session tokens. **Required** — the console refuses to start a session without it rather than falling back to a default. |
| `CONSOLE_SECURE_COOKIES` | Set `true` behind TLS so the session cookie is `Secure`. Defaults to `false` because a `Secure` cookie is silently dropped over plain http, which makes a local dev server look broken. |

Optional: `CONSOLE_TOKEN_TTL_HOURS` (default 8).

Access is **ADMIN only** — currently 2 of the 25 accounts. A `USER` account
is refused at sign-in with 403.

---

## 015a — `audit.engine_run.pipeline` renamed to `engine`

**Applied to dev:** 2026-09-22
**Applied to prod:** _not yet_

"Pipeline" already means the flow recorded in `audit.pipeline_execution_log`
(`pipeline_name`, one row per flow run). Using the same word for "which side
of the brand filter runs" gave one term two meanings in the same database.
The console's concept is which **engine** runs — Himalaya (`oneds_master`) or
Competitor (`oneds_competitor`).

If `015` has not yet been applied to prod, apply the current file and skip
this — it already creates the column as `engine`. Otherwise:

```sql
DROP INDEX IX_engine_run_scope ON audit.engine_run;
EXEC sp_rename 'audit.engine_run.pipeline', 'engine', 'COLUMN';
CREATE INDEX IX_engine_run_scope
    ON audit.engine_run (channel, engine, category, subcategory, started_at DESC);
```

`audit.pipeline_execution_log` and `pipeline_name` are **not** touched — that
is the existing meaning and it stays.
