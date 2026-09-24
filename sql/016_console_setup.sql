-- ===========================================================================
-- 016 -- Engine Console setup, as one file.
--
-- RUN THIS ONCE against the database the console will point at.
--
-- It is idempotent: every object is guarded, so re-running it is safe and
-- changes nothing the second time. It does not drop anything, and it does not
-- write a single row of data.
--
-- WHAT IT ASSUMES ALREADY EXISTS
--
-- This is a console for an engine that is already installed. It assumes the
-- target database is a working engine database and therefore already has:
--
--     staging.<channel>_products          (amazon, blinkit, swiggy, zepto)
--     staging.<channel>_product_mapping
--     staging.himalaya_products
--     config.users
--     audit.activity_log
--
-- If those are absent you are pointing at the wrong database, and the
-- verification block at the bottom will say so rather than half-installing.
--
-- WHAT IT CREATES
--
--   1. audit.engine_run            -- one row per run: scope, counts, outcome
--   2. audit.engine_run_config     -- the settings that run actually used
--   3. audit.engine_run_outcome    -- status -> count, the comparison surface
--   4. audit.vw_engine_run_compare -- before/after deltas, computed on read
--   5. review columns on the four staging.<channel>_products tables, IF and
--      ONLY IF they are missing (on the reference database they already exist)
--
-- WHAT IT DELIBERATELY DOES NOT DO
--
--   * No user is created. The console authenticates against config.users,
--     which the steward review portal also owns. Creating an account here
--     would silently grant access to that portal too. Grant console access by
--     setting an existing account's role to ADMIN -- see the note at the end.
--   * No engine table is altered beyond adding missing review columns.
--   * Nothing is dropped, and no data is written.
--
-- AFTER RUNNING THIS, two environment variables are required by the console
-- process itself (not by the database):
--
--     CONSOLE_JWT_SECRET      required; signs session tokens. The console
--                             refuses to start a session without it rather
--                             than falling back to a default.
--     CONSOLE_SECURE_COOKIES  set true behind TLS. Defaults false, because a
--                             Secure cookie is silently dropped over plain
--                             http and makes a dev server look broken.
--
-- Optional: CONSOLE_TOKEN_TTL_HOURS (default 8), CONSOLE_PORT (default 8099).
-- ===========================================================================

SET NOCOUNT ON;
GO

-- Required for the filtered index in section 5. sqlcmd connects with
-- QUOTED_IDENTIFIER OFF by default, and CREATE INDEX on a filtered index
-- refuses to run under it -- the error names seven unrelated features and
-- not the one that applies, so it is easy to misread. SSMS defaults to ON;
-- this makes the file behave the same either way.
SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;
GO

PRINT '=== Engine Console setup (016) ===';
GO


-- ===========================================================================
-- PRE-FLIGHT -- fail loudly on the wrong database rather than half-installing
-- ===========================================================================
-- All three checks in ONE batch, ending in THROW.
--
-- RAISERROR at severity 16 does not stop the script: it prints and execution
-- carries on to the next batch, so an earlier version reported three missing
-- tables and then announced "Pre-flight passed" and built everything anyway.
--
-- No sqlcmd directives here. `:on error exit` would have aborted the script
-- in sqlcmd, but it is a CLIENT directive, not T-SQL -- SSMS rejects it with
-- "Incorrect syntax near ':'" unless SQLCMD Mode is switched on, which killed
-- this whole batch and let the rest of the file run unguarded. The file has
-- to behave the same in SSMS, sqlcmd and Azure Data Studio, so the guard is
-- expressed in SQL alone: this batch writes a flag into a temp table, and
-- every following batch opens with SET NOEXEC ON unless that flag says the
-- pre-flight passed. NOEXEC makes the server parse but not run what follows,
-- which is exactly "skip the rest of the file" in every client.
--
-- The checks are reported together so a wrong-database run names every
-- problem at once rather than one per re-run.

DECLARE @missing NVARCHAR(MAX) = '';

IF SCHEMA_ID('staging') IS NULL OR OBJECT_ID('staging.himalaya_products', 'U') IS NULL
    SET @missing += CHAR(10)
        + '  * staging.himalaya_products -- the master catalogue. '
        + 'This does not look like an engine database at all.';

IF OBJECT_ID('config.users', 'U') IS NULL
    SET @missing += CHAR(10)
        + '  * config.users -- the console authenticates against it and does '
        + 'not create it; that table is owned by the steward review portal.';

IF OBJECT_ID('audit.activity_log', 'U') IS NULL
    SET @missing += CHAR(10)
        + '  * audit.activity_log -- the console writes its audit trail there '
        + '(resets, rejections) and will not create it.';

-- The flag every later batch checks. A temp table rather than a variable,
-- because a variable does not survive GO -- and GO is what separates the
-- batches this has to gate.
IF OBJECT_ID('tempdb..#console_setup') IS NOT NULL DROP TABLE #console_setup;
CREATE TABLE #console_setup (ok BIT NOT NULL);
INSERT INTO #console_setup (ok) VALUES (CASE WHEN @missing = '' THEN 1 ELSE 0 END);

IF @missing <> ''
BEGIN
    DECLARE @msg NVARCHAR(MAX) =
        'Pre-flight FAILED. Nothing will be created. Missing:' + @missing
        + CHAR(10) + 'Point this file at the engine database, or install the '
        + 'engine schema first.';
    -- THROW ends THIS batch. The temp table above is what stops the rest.
    THROW 50001, @msg, 1;
END

PRINT 'Pre-flight passed: this is an engine database.';
GO


-- ===========================================================================
-- 0 -- schema
-- ===========================================================================
IF NOT EXISTS (SELECT 1 FROM #console_setup WHERE ok = 1) SET NOEXEC ON;
IF SCHEMA_ID('audit') IS NULL
BEGIN
    EXEC('CREATE SCHEMA audit');
    PRINT 'Created schema: audit';
END
GO


-- ===========================================================================
-- 1 -- audit.engine_run
--
-- One row per run: what was asked for, what the selection resolved to, and
-- what happened.
--
-- The existing audit.pipeline_execution_log records THAT a run happened but
-- not what it did -- on the reference database, across 415 rows, triggered_by
-- and trigger_type are NULL, total_records_read is 0, total_records_failed is
-- 0 even for runs that lost 24 SKUs to deadlocks, and the scope is not stored
-- at all. "Have we run zepto lip makeup?" is unanswerable from it. That table
-- is left alone; this one answers the question.
-- ===========================================================================
IF NOT EXISTS (SELECT 1 FROM #console_setup WHERE ok = 1) SET NOEXEC ON;
IF OBJECT_ID('audit.engine_run', 'U') IS NULL
BEGIN
    CREATE TABLE audit.engine_run (
        execution_id        UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,

        -- Scope as selected. category/subcategory are NULL for an unfiltered
        -- run, which is distinct from an empty string.
        channel             VARCHAR(32)   NOT NULL,   -- amazon | blinkit | swiggy | zepto
        engine              VARCHAR(16)   NOT NULL,   -- himalaya | competitor
        category            NVARCHAR(128) NULL,
        subcategory         NVARCHAR(128) NULL,
        explicit_sku_count  INT           NULL,       -- NULL = no --sku filter

        -- Counts resolved at selection time, BEFORE any work. These are the
        -- numbers shown in the confirm-before-run dialog, kept so a later
        -- reader can tell what the operator was actually shown.
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

        -- Heartbeat. A run whose heartbeat has stopped is stale, not running.
        -- 16 rows in pipeline_execution_log sat in 'running' from 18 Aug
        -- onward with no way to tell they had died; the console reconciles
        -- against this column instead.
        heartbeat_at        DATETIME2(3) NULL,

        created_at          DATETIME2(3) NOT NULL DEFAULT SYSUTCDATETIME()
    );

    -- "Have we run this scope, and when?" -- the query the old table could
    -- not answer.
    CREATE INDEX IX_engine_run_scope
        ON audit.engine_run (channel, engine, category, subcategory, started_at DESC);

    -- Finding stale runs, and listing recent activity.
    CREATE INDEX IX_engine_run_status
        ON audit.engine_run (status, heartbeat_at);

    PRINT 'Created table: audit.engine_run';
END
ELSE
    PRINT 'Exists, skipped: audit.engine_run';
GO

-- -----------------------------------------------------------------------
-- 1a -- the pipeline -> engine rename.
--
-- Only fires on a database where an older 015 created the column as
-- "pipeline". "Pipeline" already means the flow recorded in
-- audit.pipeline_execution_log, so using the same word for "which side of the
-- brand filter runs" gave one term two meanings in one database. The console
-- reads `engine` and nothing else.
-- -----------------------------------------------------------------------
IF NOT EXISTS (SELECT 1 FROM #console_setup WHERE ok = 1) SET NOEXEC ON;
IF  COL_LENGTH('audit.engine_run', 'pipeline') IS NOT NULL
AND COL_LENGTH('audit.engine_run', 'engine')   IS NULL
BEGIN
    IF EXISTS (SELECT 1 FROM sys.indexes
               WHERE name = 'IX_engine_run_scope'
                 AND object_id = OBJECT_ID('audit.engine_run'))
        DROP INDEX IX_engine_run_scope ON audit.engine_run;

    EXEC sp_rename 'audit.engine_run.pipeline', 'engine', 'COLUMN';

    CREATE INDEX IX_engine_run_scope
        ON audit.engine_run (channel, engine, category, subcategory, started_at DESC);

    PRINT 'Renamed audit.engine_run.pipeline -> engine';
END
GO


-- ===========================================================================
-- 2 -- audit.engine_run_config
--
-- The settings a run actually used. Key/value rather than columns: the
-- tunables in common/config.py change over time, and a schema migration per
-- new scoring weight would guarantee this stops being filled in.
--
-- An outcome delta is not interpretable without this. "+5 AutoMatch" could
-- come from a rules.py fix, a weight change, or different input data.
-- ===========================================================================
IF NOT EXISTS (SELECT 1 FROM #console_setup WHERE ok = 1) SET NOEXEC ON;
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
    PRINT 'Created table: audit.engine_run_config';
END
ELSE
    PRINT 'Exists, skipped: audit.engine_run_config';
GO


-- ===========================================================================
-- 3 -- audit.engine_run_outcome
--
-- status -> count. This is the comparison surface: two runs of the same scope
-- joined on status give the before/after delta.
--
-- DELIBERATE SCOPE LIMIT: no per-SKU rows are stored. The aggregate answers
-- "what changed" but never "which SKUs changed", and it cannot distinguish
-- +5 net from (+8 gained, -3 lost) -- a run that fixes eight matches and
-- breaks three reads here as a clean "+5". Drilling into which rows moved is
-- done against staging.<channel>_product_mapping while the result is live.
-- ===========================================================================
IF NOT EXISTS (SELECT 1 FROM #console_setup WHERE ok = 1) SET NOEXEC ON;
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
    PRINT 'Created table: audit.engine_run_outcome';
END
ELSE
    PRINT 'Exists, skipped: audit.engine_run_outcome';
GO


-- ===========================================================================
-- 4 -- audit.vw_engine_run_compare
--
-- Deltas computed on read, never stored -- a stored diff would be a third
-- copy of the same facts, able to disagree with them.
--
--   SELECT * FROM audit.vw_engine_run_compare
--   WHERE before_run = '<id>' AND after_run = '<id>';
--
-- FULL JOIN, not INNER: a status present in only one of the two runs is
-- exactly the interesting case (Failed 2 -> 0 must still appear), and an
-- INNER JOIN would silently drop it.
--
-- CREATE OR ALTER so a re-run refreshes the definition without a drop.
-- ===========================================================================
-- Built through EXEC rather than written inline.
--
-- Two reasons, both learned the hard way. CREATE VIEW must be the first
-- statement in its batch, so it cannot share one with the NOEXEC guard the
-- CREATE TABLE batches use. And NOEXEC stops execution but NOT compilation,
-- so an inline CREATE VIEW still gets bound against audit.engine_run and
-- reports "Invalid object name" on a database where the pre-flight already
-- refused to create it -- an error about a table nobody asked for. Inside
-- EXEC the body is just a string until the guard has let us get this far.
IF NOT EXISTS (SELECT 1 FROM #console_setup WHERE ok = 1) SET NOEXEC ON;

EXEC('
CREATE OR ALTER VIEW audit.vw_engine_run_compare
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
    -- Doubled quotes: this whole body is a string literal passed to EXEC.
    CASE WHEN ISNULL(b.git_sha,'''') <> ISNULL(a.git_sha,'''') THEN 1 ELSE 0 END
                                        AS code_changed
FROM audit.engine_run b
CROSS JOIN audit.engine_run a
LEFT  JOIN audit.engine_run_outcome ob ON ob.execution_id = b.execution_id
FULL  JOIN audit.engine_run_outcome oa ON oa.execution_id = a.execution_id
                                      AND oa.status = ob.status
WHERE b.execution_id <> a.execution_id;
');

PRINT 'Created or refreshed view: audit.vw_engine_run_compare';
GO


-- ===========================================================================
-- 5 -- review columns on the source tables
--
-- The console's Rejected page records a steward's verdict in these columns.
-- On the reference database all four already carry them, so this block is
-- normally a no-op -- it exists so a database provisioned from an older
-- schema does not fail at runtime with "invalid column name review_status".
--
-- NO DEFAULT AND NO BACKFILL. review_status is NULL on every unreviewed row,
-- and that NULL is meaningful: the console distinguishes "nobody has looked"
-- from "reviewed and approved" from "reviewed and rejected". Writing a
-- default would erase the first of those three states.
-- ===========================================================================
IF NOT EXISTS (SELECT 1 FROM #console_setup WHERE ok = 1) SET NOEXEC ON;
-- Re-stated for this batch: SET options do not carry across GO, and the
-- filtered index below refuses to be created under QUOTED_IDENTIFIER OFF.
SET QUOTED_IDENTIFIER ON;
SET ANSI_NULLS ON;

DECLARE @tbl SYSNAME, @sql NVARCHAR(MAX);
DECLARE channel_cur CURSOR LOCAL FAST_FORWARD FOR
    SELECT v.name FROM (VALUES
        ('amazon_products'), ('blinkit_products'),
        ('swiggy_products'), ('zepto_products')
    ) AS v(name);

OPEN channel_cur;
FETCH NEXT FROM channel_cur INTO @tbl;

WHILE @@FETCH_STATUS = 0
BEGIN
    IF OBJECT_ID('staging.' + @tbl, 'U') IS NULL
    BEGIN
        PRINT 'WARNING: staging.' + @tbl + ' does not exist -- that channel will '
            + 'fail in the console until it is created.';
    END
    ELSE
    BEGIN
        IF COL_LENGTH('staging.' + @tbl, 'review_status') IS NULL
        BEGIN
            SET @sql = 'ALTER TABLE staging.' + QUOTENAME(@tbl)
                     + ' ADD review_status NVARCHAR(32) NULL';
            EXEC sp_executesql @sql;
            PRINT 'Added staging.' + @tbl + '.review_status';
        END

        IF COL_LENGTH('staging.' + @tbl, 'reviewer_comment') IS NULL
        BEGIN
            SET @sql = 'ALTER TABLE staging.' + QUOTENAME(@tbl)
                     + ' ADD reviewer_comment NVARCHAR(1000) NULL';
            EXEC sp_executesql @sql;
            PRINT 'Added staging.' + @tbl + '.reviewer_comment';
        END

        IF COL_LENGTH('staging.' + @tbl, 'reviewed_by') IS NULL
        BEGIN
            SET @sql = 'ALTER TABLE staging.' + QUOTENAME(@tbl)
                     + ' ADD reviewed_by NVARCHAR(128) NULL';
            EXEC sp_executesql @sql;
            PRINT 'Added staging.' + @tbl + '.reviewed_by';
        END

        IF COL_LENGTH('staging.' + @tbl, 'reviewed_at') IS NULL
        BEGIN
            SET @sql = 'ALTER TABLE staging.' + QUOTENAME(@tbl)
                     + ' ADD reviewed_at DATETIME2(3) NULL';
            EXEC sp_executesql @sql;
            PRINT 'Added staging.' + @tbl + '.reviewed_at';
        END

        -- Rejected listings are read by review_status across the whole table,
        -- and a filtered index keeps that cheap on amazon's 154k rows without
        -- carrying an entry for the ~99.5% that are NULL.
        --
        -- Wrapped in TRY/CATCH because the index is an optimisation and the
        -- columns above are the requirement. A filtered index refuses to be
        -- created under QUOTED_IDENTIFIER OFF, and letting that abort the
        -- batch once left a channel without its review columns at all --
        -- a console that then fails at runtime with "invalid column name"
        -- rather than merely running one query more slowly.
        IF NOT EXISTS (SELECT 1 FROM sys.indexes
                       WHERE name = 'IX_' + @tbl + '_review_status'
                         AND object_id = OBJECT_ID('staging.' + @tbl))
        BEGIN
            BEGIN TRY
                SET @sql = 'CREATE INDEX ' + QUOTENAME('IX_' + @tbl + '_review_status')
                         + ' ON staging.' + QUOTENAME(@tbl) + ' (review_status) '
                         + ' WHERE review_status IS NOT NULL';
                EXEC sp_executesql @sql;
                PRINT 'Created index IX_' + @tbl + '_review_status';
            END TRY
            BEGIN CATCH
                PRINT 'NOTE: could not create IX_' + @tbl + '_review_status ('
                    + ERROR_MESSAGE() + '). The console works without it; '
                    + 'the Rejected page will just scan instead of seek.';
            END CATCH
        END
    END

    FETCH NEXT FROM channel_cur INTO @tbl;
END

CLOSE channel_cur;
DEALLOCATE channel_cur;
GO


-- ===========================================================================
-- VERIFICATION -- what the console will find when it starts
-- ===========================================================================
IF NOT EXISTS (SELECT 1 FROM #console_setup WHERE ok = 1) SET NOEXEC ON;
PRINT '';
PRINT '=== Verification ===';
GO

SELECT
    required.object_name,
    required.purpose,
    CASE WHEN required.present = 1 THEN 'OK' ELSE '*** MISSING ***' END AS state
FROM (
    SELECT 'audit.engine_run'             AS object_name,
           'Run history'                  AS purpose,
           CASE WHEN OBJECT_ID('audit.engine_run','U')             IS NULL THEN 0 ELSE 1 END AS present
    UNION ALL SELECT 'audit.engine_run_config',    'Settings per run',
           CASE WHEN OBJECT_ID('audit.engine_run_config','U')      IS NULL THEN 0 ELSE 1 END
    UNION ALL SELECT 'audit.engine_run_outcome',   'Comparison surface',
           CASE WHEN OBJECT_ID('audit.engine_run_outcome','U')     IS NULL THEN 0 ELSE 1 END
    UNION ALL SELECT 'audit.vw_engine_run_compare','Run comparison view',
           CASE WHEN OBJECT_ID('audit.vw_engine_run_compare','V')  IS NULL THEN 0 ELSE 1 END
    UNION ALL SELECT 'audit.activity_log',         'Audit trail (pre-existing)',
           CASE WHEN OBJECT_ID('audit.activity_log','U')           IS NULL THEN 0 ELSE 1 END
    UNION ALL SELECT 'config.users',               'Sign-in (pre-existing)',
           CASE WHEN OBJECT_ID('config.users','U')                 IS NULL THEN 0 ELSE 1 END
    UNION ALL SELECT 'staging.himalaya_products',  'Master catalogue (pre-existing)',
           CASE WHEN OBJECT_ID('staging.himalaya_products','U')    IS NULL THEN 0 ELSE 1 END
) AS required
ORDER BY required.present, required.object_name;
GO

-- Per-channel readiness. A channel missing its tables is not fatal -- the
-- console simply cannot run it -- so this reports rather than raises.
SELECT
    v.channel,
    CASE WHEN OBJECT_ID('staging.' + v.channel + '_products','U') IS NULL
         THEN '*** MISSING ***' ELSE 'OK' END        AS products_table,
    CASE WHEN OBJECT_ID('staging.' + v.channel + '_product_mapping','U') IS NULL
         THEN '*** MISSING ***' ELSE 'OK' END        AS mapping_table,
    CASE WHEN COL_LENGTH('staging.' + v.channel + '_products','review_status') IS NULL
         THEN '*** MISSING ***' ELSE 'OK' END        AS review_status,
    CASE WHEN COL_LENGTH('staging.' + v.channel + '_products','mapping_status') IS NULL
         THEN '*** MISSING ***' ELSE 'OK' END        AS mapping_status
FROM (VALUES ('amazon'), ('blinkit'), ('swiggy'), ('zepto')) AS v(channel);
GO

-- Who can sign in. The console is ADMIN-only: a USER account is refused at
-- sign-in with 403. If this returns no rows, nobody can get in -- see below.
PRINT '';
PRINT 'Accounts that will be able to sign in to the console (role = ADMIN):';
GO

SELECT email, name, role, status AS is_active
FROM config.users
WHERE UPPER(LTRIM(RTRIM(role))) = 'ADMIN'
ORDER BY email;
GO


-- ===========================================================================
-- GRANTING ACCESS -- read this before running anything below
--
-- The console authenticates against config.users, which the steward review
-- portal ALSO owns. That is why this file creates no account: an INSERT here
-- grants access to that portal too, and a password set here overwrites
-- whatever the portal had.
--
-- To grant console access, promote an account that already exists:
--
--     UPDATE config.users
--     SET role = 'ADMIN', updated_at = SYSUTCDATETIME()
--     WHERE email = 'someone@covalenseglobal.com';
--
-- Verify first that the account is the one you mean:
--
--     SELECT email, name, role, status FROM config.users
--     WHERE email = 'someone@covalenseglobal.com';
--
-- Password hashes are argon2id and are set through the portal, never here.
-- ===========================================================================

-- NOEXEC is a SESSION setting: left on, every statement the operator ran
-- next in the same window would be parsed and silently discarded.
SET NOEXEC OFF;
GO

IF NOT EXISTS (SELECT 1 FROM #console_setup WHERE ok = 1)
BEGIN
    PRINT '';
    PRINT '*** 016 did NOT run: pre-flight failed. See the error above. ***';
    PRINT 'Nothing was created.';
END
ELSE
BEGIN
    PRINT '';
    PRINT '=== 016 complete ===';
    PRINT 'Next: set CONSOLE_JWT_SECRET in the console environment (required),';
    PRINT 'and CONSOLE_SECURE_COOKIES=true if it is served over TLS.';
END
GO

-- Tidied up, so a second run in the same session starts from a clean flag
-- rather than reading the last run's verdict.
IF OBJECT_ID('tempdb..#console_setup') IS NOT NULL DROP TABLE #console_setup;
GO
