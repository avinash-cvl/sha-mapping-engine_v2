/* ============================================================================
   012 - TP/FP calibration
   ============================================================================

   WHY THIS EXISTS

   Every scored row carries two independent opinions:

     ensemble_score   deterministic, from stage_scoring.py's weighted signals
     llm_confidence   the MCDA judge's own certainty

   Neither is reliable on its own. Both failure directions are measured on
   live data:

     B0CD466FHY  "tan removal orange peel off mask, 8gm, Pack of 12"
                 ensemble 0.23, LLM 0.99 -> the LLM was RIGHT (exact match on
                 product, size and count; the ensemble ranked a turmeric face
                 pack above it)

     B000N33STO  "Skin Wellness Tablets - 60 Count (Neem)"
                 ensemble 0.71, LLM 0.94 -> the LLM was WRONG (picked the
                 500-count master over the correct 60-count)

   Today determine_mapping_status() arbitrates with max(ensemble, llm) plus a
   hand-set 0.60 floor. That constant was never validated against outcomes --
   it cannot be right for both rows above, and it is not derived from anything.

   This table replaces the constant with measured evidence: for each
   (ensemble band, LLM band) bucket, what share of human-reviewed rows in that
   bucket were actually APPROVED. That share is a real probability, unlike the
   weighted average it replaces -- a calibrated score of 0.84 means "rows that
   looked like this were correct 84% of the time".

   SAFETY

   A bucket is only used once it has at least min_labels behind it. Below that
   threshold is_active stays 0 and the engine falls back to today's logic, so
   this is incremental and reversible rather than a big-bang switch.

   Run once. Idempotent -- safe to re-run.
   ============================================================================ */

SET NOCOUNT ON;
GO

/* ----------------------------------------------------------------------------
   1. config.calibration_buckets
   --------------------------------------------------------------------------*/
IF OBJECT_ID('config.calibration_buckets', 'U') IS NULL
BEGIN
    CREATE TABLE config.calibration_buckets
    (
        id                  INT IDENTITY(1,1)   NOT NULL,

        -- Bucket key. 'low' | 'mid' | 'high', derived from the band
        -- boundaries recorded below so a rebuild is reproducible even if the
        -- thresholds are later retuned.
        ensemble_band       VARCHAR(10)         NOT NULL,
        llm_band            VARCHAR(10)         NOT NULL,

        -- Evidence behind this bucket.
        n_labelled          INT                 NOT NULL CONSTRAINT DF_calib_n_labelled  DEFAULT (0),
        n_approved          INT                 NOT NULL CONSTRAINT DF_calib_n_approved  DEFAULT (0),
        n_rejected          INT                 NOT NULL CONSTRAINT DF_calib_n_rejected  DEFAULT (0),

        -- n_approved / n_labelled. THE calibrated score for rows in this
        -- bucket. NULL until there is anything to divide.
        precision_rate      DECIMAL(8,4)        NULL,

        -- Wilson 95% lower bound. Use this, not precision_rate, when deciding
        -- whether a bucket is safe to auto-match on: 3/3 is 100% and means
        -- nothing, while 280/300 is 93% and means a great deal.
        precision_lower_95  DECIMAL(8,4)        NULL,

        -- Minimum labels before this bucket is trusted. 30 gives roughly
        -- +/- 9 points at 95% confidence, which is the coarsest estimate
        -- still worth acting on.
        min_labels          INT                 NOT NULL CONSTRAINT DF_calib_min_labels  DEFAULT (30),

        -- 0 = fall back to the existing max()/floor logic for this bucket.
        -- Set by the rebuild script, never by hand.
        is_active           BIT                 NOT NULL CONSTRAINT DF_calib_is_active   DEFAULT (0),

        -- The band boundaries this row was computed under. Without these a
        -- retune silently invalidates every stored rate with no way to tell.
        band_high_cutoff    DECIMAL(8,4)        NOT NULL CONSTRAINT DF_calib_high_cut    DEFAULT (0.86),
        band_mid_cutoff     DECIMAL(8,4)        NOT NULL CONSTRAINT DF_calib_mid_cut     DEFAULT (0.61),

        -- Provenance: which scoring run produced the scores behind these
        -- counts. Labels are only comparable to scores from the same run --
        -- mixing an August score with a September verdict is the main way a
        -- calibration table goes quietly wrong.
        source_run_note     VARCHAR(200)        NULL,

        created_at          DATETIME2(3)        NOT NULL CONSTRAINT DF_calib_created     DEFAULT (SYSUTCDATETIME()),
        updated_at          DATETIME2(3)        NULL,
        updated_by          VARCHAR(100)        NULL,

        CONSTRAINT PK_calibration_buckets       PRIMARY KEY CLUSTERED (id),
        CONSTRAINT UQ_calibration_buckets_band  UNIQUE (ensemble_band, llm_band),
        CONSTRAINT CK_calib_ens_band            CHECK (ensemble_band IN ('low','mid','high')),
        CONSTRAINT CK_calib_llm_band            CHECK (llm_band      IN ('low','mid','high','none')),
        CONSTRAINT CK_calib_counts              CHECK (n_approved + n_rejected <= n_labelled),
        CONSTRAINT CK_calib_rate                CHECK (precision_rate IS NULL
                                                      OR (precision_rate >= 0 AND precision_rate <= 1))
    );

    PRINT 'Created config.calibration_buckets';
END
ELSE
    PRINT 'config.calibration_buckets already exists - skipped';
GO

/* Seed the 12 bucket keys so the table is complete and inspectable before any
   labels exist. All inactive, all counts zero. llm_band 'none' covers rows
   the judge never ran on (needs_judge() was false) -- those are ensemble-only
   and must not be silently lumped in with a judged bucket. */
IF NOT EXISTS (SELECT 1 FROM config.calibration_buckets)
BEGIN
    INSERT INTO config.calibration_buckets (ensemble_band, llm_band, source_run_note)
    SELECT e.band, l.band, 'seeded - awaiting labels'
    FROM   (VALUES ('low'), ('mid'), ('high'))                AS e(band)
    CROSS JOIN (VALUES ('low'), ('mid'), ('high'), ('none'))  AS l(band);

    PRINT 'Seeded 12 calibration buckets (all inactive)';
END
GO

/* ----------------------------------------------------------------------------
   2. Per-row calibration columns on the four channel mapping tables

   Nullable with no default: a row written before calibration is enabled, or
   one whose bucket is still inactive, simply carries NULL and the existing
   logic decides its status. Nothing downstream is required to read these.
   --------------------------------------------------------------------------*/
DECLARE @tbl SYSNAME, @sql NVARCHAR(MAX);

DECLARE tables CURSOR LOCAL FAST_FORWARD FOR
    SELECT name FROM sys.tables
    WHERE  schema_id = SCHEMA_ID('staging')
    AND    name IN ('amazon_product_mapping',
                    'blinkit_product_mapping',
                    'swiggy_product_mapping',
                    'zepto_product_mapping');

OPEN tables;
FETCH NEXT FROM tables INTO @tbl;

WHILE @@FETCH_STATUS = 0
BEGIN
    -- The bucket's precision, carried onto the row that used it. This is what
    -- the portal should display instead of final_score once calibration is on.
    IF COL_LENGTH('staging.' + @tbl, 'calibrated_score') IS NULL
    BEGIN
        SET @sql = 'ALTER TABLE staging.' + QUOTENAME(@tbl)
                 + ' ADD calibrated_score DECIMAL(8,4) NULL;';
        EXEC sp_executesql @sql;
        PRINT '  + ' + @tbl + '.calibrated_score';
    END

    -- e.g. 'low/high'. Makes every decision auditable after the fact.
    IF COL_LENGTH('staging.' + @tbl, 'calibration_bucket') IS NULL
    BEGIN
        SET @sql = 'ALTER TABLE staging.' + QUOTENAME(@tbl)
                 + ' ADD calibration_bucket VARCHAR(20) NULL;';
        EXEC sp_executesql @sql;
        PRINT '  + ' + @tbl + '.calibration_bucket';
    END

    -- How many labels backed that bucket at write time. A score from 12
    -- labels is not the same claim as one from 400, and the row should
    -- remember which it was.
    IF COL_LENGTH('staging.' + @tbl, 'calibration_n') IS NULL
    BEGIN
        SET @sql = 'ALTER TABLE staging.' + QUOTENAME(@tbl)
                 + ' ADD calibration_n INT NULL;';
        EXEC sp_executesql @sql;
        PRINT '  + ' + @tbl + '.calibration_n';
    END

    FETCH NEXT FROM tables INTO @tbl;
END

CLOSE tables;
DEALLOCATE tables;
GO

PRINT 'Migration 012 complete.';
GO

/* ----------------------------------------------------------------------------
   VERIFY
   --------------------------------------------------------------------------*/
SELECT ensemble_band, llm_band, n_labelled, n_approved,
       precision_rate, precision_lower_95, is_active
FROM   config.calibration_buckets
ORDER  BY ensemble_band, llm_band;

SELECT t.name AS table_name, c.name AS column_name
FROM   sys.tables t
JOIN   sys.columns c ON c.object_id = t.object_id
WHERE  t.schema_id = SCHEMA_ID('staging')
AND    c.name IN ('calibrated_score','calibration_bucket','calibration_n')
ORDER  BY t.name, c.name;
GO
