/* ============================================================================
   014 - identity / v2 scoring columns
   ============================================================================

   WHY

   The engine now produces four numbers where it used to produce two, and they
   answer different questions:

     ensemble_score    how SIMILAR the two rows look (six text/vector signals)
     llm_score         how confident the judge is that they are one product
     attribute_score   how much of the parsed identity evidence agrees
     final_score_v2    the blend, with the identity overrides applied

   Keeping them apart is the point. A row can be the same sellable unit and
   still look dissimilar: measured, "Tan Removal Orange Peel Off Mask, 8gm,
   Pack of 12" against "TAN REMOVAL ORANGE PEEL OFF MASK 8G 1X12N SACHET"
   scored an ensemble of 0.23 -- below a turmeric face pack -- while its
   attribute_score was 1.00. Collapsing those into one column would destroy
   exactly the signal that explains the decision.

   NOTHING IS OVERWRITTEN

   ensemble_score and final_score keep their existing meaning and values.
   final_score_v2 is a NEW column, deliberately not a replacement, so a run
   can be compared against the old logic on the same rows.

   All columns are nullable with no default: rows written before this
   migration, and rows whose identity path did not run, simply carry NULL.

   Run once. Idempotent -- safe to re-run.
   ============================================================================ */

SET NOCOUNT ON;
GO

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
    PRINT 'staging.' + @tbl;

    -- 0-1. Weighted agreement over the identity attributes that were actually
    -- comparable on this pair (brand, family, form, size, count), renormalised
    -- so an attribute neither side states neither helps nor hurts.
    IF COL_LENGTH('staging.' + @tbl, 'attribute_score') IS NULL
    BEGIN
        SET @sql = 'ALTER TABLE staging.' + QUOTENAME(@tbl)
                 + ' ADD attribute_score DECIMAL(8,4) NULL;';
        EXEC sp_executesql @sql;
        PRINT '  + attribute_score';
    END

    -- The judge's own confidence, stored separately from final_score.
    -- final_score currently holds max(ensemble, llm), so the LLM's actual
    -- number is not recoverable from it -- which made it impossible to ask
    -- afterwards how often the judge alone was right.
    IF COL_LENGTH('staging.' + @tbl, 'llm_score') IS NULL
    BEGIN
        SET @sql = 'ALTER TABLE staging.' + QUOTENAME(@tbl)
                 + ' ADD llm_score DECIMAL(8,4) NULL;';
        EXEC sp_executesql @sql;
        PRINT '  + llm_score';
    END

    -- The v2 blend after the identity overrides. A SEPARATE column from
    -- final_score on purpose: both are written on the same run so the old and
    -- new dispositions can be compared row by row before either is trusted.
    IF COL_LENGTH('staging.' + @tbl, 'final_score_v2') IS NULL
    BEGIN
        SET @sql = 'ALTER TABLE staging.' + QUOTENAME(@tbl)
                 + ' ADD final_score_v2 DECIMAL(8,4) NULL;';
        EXEC sp_executesql @sql;
        PRINT '  + final_score_v2';
    END

    -- What actually decided this row: DETERMINISTIC, IDENTITY+LLM, ENSEMBLE,
    -- CONFLICT_CAPPED. Without it a score of 1.0 is indistinguishable from a
    -- blend that happened to reach 1.0, and no one can audit why a row landed
    -- where it did.
    IF COL_LENGTH('staging.' + @tbl, 'match_method') IS NULL
    BEGIN
        SET @sql = 'ALTER TABLE staging.' + QUOTENAME(@tbl)
                 + ' ADD match_method VARCHAR(40) NULL;';
        EXEC sp_executesql @sql;
        PRINT '  + match_method';
    END

    -- 1 when a positively-established disagreement blocked this pair. The
    -- guard that stops a confident judge promoting a 100ml listing onto a
    -- 500ml master; recorded so the block is visible rather than implicit in
    -- a suppressed score.
    IF COL_LENGTH('staging.' + @tbl, 'critical_conflict') IS NULL
    BEGIN
        SET @sql = 'ALTER TABLE staging.' + QUOTENAME(@tbl)
                 + ' ADD critical_conflict BIT NULL;';
        EXEC sp_executesql @sql;
        PRINT '  + critical_conflict';
    END

    -- 1 when the parsed attributes say this IS the same sellable unit.
    IF COL_LENGTH('staging.' + @tbl, 'identity_match') IS NULL
    BEGIN
        SET @sql = 'ALTER TABLE staging.' + QUOTENAME(@tbl)
                 + ' ADD identity_match BIT NULL;';
        EXEC sp_executesql @sql;
        PRINT '  + identity_match';
    END

    -- Human-readable trace, e.g.
    --   'identity attr=1.00 agree=brand,family,form,size,count'
    --   'no-identity attr=0.71 agree=brand,family,form CONFLICT=pack_size'
    -- so a steward can see WHICH attributes drove the verdict without
    -- re-running anything.
    IF COL_LENGTH('staging.' + @tbl, 'identity_evidence') IS NULL
    BEGIN
        SET @sql = 'ALTER TABLE staging.' + QUOTENAME(@tbl)
                 + ' ADD identity_evidence VARCHAR(400) NULL;';
        EXEC sp_executesql @sql;
        PRINT '  + identity_evidence';
    END

    FETCH NEXT FROM tables INTO @tbl;
END

CLOSE tables;
DEALLOCATE tables;
GO

PRINT 'Migration 014 complete.';
GO

/* ----------------------------------------------------------------------------
   VERIFY
   --------------------------------------------------------------------------*/
SELECT t.name AS table_name, c.name AS column_name, ty.name AS data_type
FROM   sys.tables t
JOIN   sys.columns c  ON c.object_id = t.object_id
JOIN   sys.types  ty  ON ty.user_type_id = c.user_type_id
WHERE  t.schema_id = SCHEMA_ID('staging')
AND    c.name IN ('attribute_score','llm_score','final_score_v2',
                  'match_method','critical_conflict','identity_match',
                  'identity_evidence')
ORDER  BY t.name, c.name;
GO
