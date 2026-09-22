-- ===========================================================================
-- 015 -- Engine run history: scope, config and outcome, per run.
--
-- WHY THIS EXISTS
--
-- persist_sku_disposition() DELETES and re-inserts a SKU's mapping rows on
-- every run (see batch_flow_steps.py, "deletes+inserts that SKU's mapping
-- rows"), so staging.*_product_mapping only ever holds the LATEST answer --
-- measured: every source row carries exactly one distinct batch_id. Re-running
-- after an engine fix therefore destroys the result it should be compared
-- against.
--
-- audit.pipeline_execution_log already records that a run happened, but not
-- what it did: across 415 rows, triggered_by and trigger_type are NULL,
-- total_records_read is 0, total_records_failed is 0 even for runs that lost
-- 24 SKUs to deadlocks, and the scope (category / sub-category / brand side)
-- is not stored at all. "Have we run zepto lip makeup?" is unanswerable from
-- it.
--
-- These three tables close that gap at the AGGREGATE level only.
--
-- DELIBERATE SCOPE LIMIT
--
-- No per-SKU rows are stored. That is a considered trade, not an oversight:
-- the aggregate answers "what changed" but never "which SKUs changed", and it
-- cannot distinguish +5 net from (+8 gained, -3 lost) -- a run that fixes
-- eight matches and breaks three reads here as a clean "+5". Drilling into
-- which rows moved is done manually against staging.*_product_mapping while
-- the result is still live. If that trade stops paying, add a per-SKU
-- snapshot (sku, rank1 product_code, status) -- roughly 600 KB gzipped per
-- full-channel run -- rather than widening these tables.
--
-- WHY CONFIG IS CAPTURED TOO
--
-- An outcome delta is not interpretable without the settings that produced
-- it: "+5 AutoMatch" could come from a rules.py fix, a weight change, or
-- different input data. git_sha pins rules.py and the scoring code;
-- source_row_count pins the input population, so a comparison spanning a data
-- change (the 21 Sep duplicate delete removed 183,657 rows) is detectable
-- rather than silently misleading.
-- ===========================================================================

IF SCHEMA_ID('audit') IS NULL
    EXEC('CREATE SCHEMA audit');
GO

-- ---------------------------------------------------------------------------
-- One row per run: what was asked for, and what the selection resolved to.
-- ---------------------------------------------------------------------------
IF OBJECT_ID('audit.engine_run', 'U') IS NULL
BEGIN
    CREATE TABLE audit.engine_run (
        execution_id        UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,

        -- Scope as selected. category/subcategory are NULL for an
        -- unfiltered run, which is distinct from an empty string.
        channel             VARCHAR(32)   NOT NULL,   -- amazon | blinkit | swiggy | zepto
        pipeline            VARCHAR(16)   NOT NULL,   -- himalaya | competitor
        category            NVARCHAR(128) NULL,
        subcategory         NVARCHAR(128) NULL,
        explicit_sku_count  INT           NULL,       -- NULL = no --sku filter

        -- Counts resolved at selection time, BEFORE any work. These are the
        -- numbers the console shows for confirm-before-run, kept so a later
        -- reader can tell what the operator was shown.
        scoped_total        INT NOT NULL DEFAULT 0,   -- rows matching the scope
        to_run              INT NOT NULL DEFAULT 0,   -- PENDING and not approved
        approved_skipped    INT NOT NULL DEFAULT 0,   -- steward-approved, never touched
        already_mapped      INT NOT NULL DEFAULT 0,
        failed_reset        INT NOT NULL DEFAULT 0,   -- Failed rows reset into this run

        -- Input population at run time. A comparison across a change in this
        -- number is comparing different data, not different engine behaviour.
        source_row_count    INT NULL,

        -- Execution
        started_at          DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME(),
        ended_at            DATETIME2(3) NULL,
        status              VARCHAR(24)  NOT NULL DEFAULT 'running',
                            -- running | completed | completed_with_failures
                            -- | cancelled | failed | stale
        processed           INT NOT NULL DEFAULT 0,
        failed              INT NOT NULL DEFAULT 0,

        -- Provenance. triggered_by is the authenticated user for a
        -- console-launched run; NULL only for a CLI run that predates it.
        triggered_by        NVARCHAR(128) NULL,
        trigger_type        VARCHAR(16)   NULL,       -- manual | scheduled | cli
        git_sha             VARCHAR(40)   NULL,       -- pins rules.py + scoring
        error_message       NVARCHAR(MAX) NULL,

        -- Heartbeat. A run whose heartbeat has stopped is stale, not running
        -- -- 16 rows in pipeline_execution_log sat in 'running' from 18 Aug
        -- onward with no way to tell they had died.
        heartbeat_at        DATETIME2(3) NULL,

        created_at          DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME()
    );

    -- "Have we run this scope, and when?" -- the query the old table could
    -- not answer.
    CREATE INDEX IX_engine_run_scope
        ON audit.engine_run (channel, pipeline, category, subcategory, started_at DESC);

    -- Finding stale runs, and listing recent activity.
    CREATE INDEX IX_engine_run_status
        ON audit.engine_run (status, heartbeat_at);
END
GO

-- ---------------------------------------------------------------------------
-- Config as it actually was, per run. Key/value rather than columns: the
-- tunables in common/config.py change over time, and a schema migration per
-- new weight would guarantee this stops being filled in.
-- ---------------------------------------------------------------------------
IF OBJECT_ID('audit.engine_run_config', 'U') IS NULL
BEGIN
    CREATE TABLE audit.engine_run_config (
        execution_id    UNIQUEIDENTIFIER NOT NULL,
        config_key      VARCHAR(64)      NOT NULL,   -- TOP_N_OUTPUT, W_PACK, ...
        config_value    NVARCHAR(256)    NOT NULL,
        is_override     BIT              NOT NULL DEFAULT 0,  -- differed from the default
        CONSTRAINT PK_engine_run_config PRIMARY KEY (execution_id, config_key),
        CONSTRAINT FK_engine_run_config_run FOREIGN KEY (execution_id)
            REFERENCES audit.engine_run (execution_id) ON DELETE CASCADE
    );
END
GO

-- ---------------------------------------------------------------------------
-- Outcome: status -> count. This is the comparison surface -- two runs of the
-- same scope joined on status give the before/after delta.
-- ---------------------------------------------------------------------------
IF OBJECT_ID('audit.engine_run_outcome', 'U') IS NULL
BEGIN
    CREATE TABLE audit.engine_run_outcome (
        execution_id    UNIQUEIDENTIFIER NOT NULL,
        status          VARCHAR(32)      NOT NULL,   -- AutoMatch | StewardReview
                                                     -- | LowConfidence
                                                     -- | NoHimalayaEquivalent | Failed
        sku_count       INT              NOT NULL,
        CONSTRAINT PK_engine_run_outcome PRIMARY KEY (execution_id, status),
        CONSTRAINT FK_engine_run_outcome_run FOREIGN KEY (execution_id)
            REFERENCES audit.engine_run (execution_id) ON DELETE CASCADE
    );
END
GO

-- ---------------------------------------------------------------------------
-- Comparison view. Deltas are computed on read, never stored -- a stored diff
-- would be a third copy of the same facts, able to disagree with them.
--
--   SELECT * FROM audit.vw_engine_run_compare
--   WHERE before_run = '<id>' AND after_run = '<id>';
--
-- FULL JOIN, not INNER: a status present in only one of the two runs is
-- exactly the interesting case (Failed 2 -> 0 must still appear), and an
-- INNER JOIN would silently drop it.
-- ---------------------------------------------------------------------------
IF OBJECT_ID('audit.vw_engine_run_compare', 'V') IS NOT NULL
    DROP VIEW audit.vw_engine_run_compare;
GO

CREATE VIEW audit.vw_engine_run_compare
AS
SELECT
    b.execution_id                      AS before_run,
    a.execution_id                      AS after_run,
    COALESCE(ob.status, oa.status)      AS status,
    ISNULL(ob.sku_count, 0)             AS before_count,
    ISNULL(oa.sku_count, 0)             AS after_count,
    ISNULL(oa.sku_count, 0) - ISNULL(ob.sku_count, 0) AS delta,
    -- Flags that a delta may not be engine behaviour at all.
    CASE WHEN b.source_row_count <> a.source_row_count THEN 1 ELSE 0 END
                                        AS input_population_changed,
    CASE WHEN ISNULL(b.git_sha,'') <> ISNULL(a.git_sha,'') THEN 1 ELSE 0 END
                                        AS code_changed
FROM audit.engine_run b
CROSS JOIN audit.engine_run a
LEFT  JOIN audit.engine_run_outcome ob ON ob.execution_id = b.execution_id
FULL  JOIN audit.engine_run_outcome oa ON oa.execution_id = a.execution_id
                                      AND oa.status = ob.status
WHERE b.execution_id <> a.execution_id;
GO
