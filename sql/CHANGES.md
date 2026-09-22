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
