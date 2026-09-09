/* ============================================================================
   013 - OLD baseline snapshot
   ============================================================================

   WHY A TABLE AND NOT A FILE

   Earlier in this work a JSON snapshot was the only record of a run's results,
   and a stored procedure reload wiped the mapping tables overnight -- the
   scores were unrecoverable because nothing in the database held them. A
   snapshot that lives beside the data it describes survives the same accident,
   and can be joined to rather than parsed.

   WHY A SEPARATE TABLE AND NOT SYSTEM-VERSIONING

   Temporal tables would version every write, including the thousands of
   intermediate rows a re-run produces. What is wanted here is one deliberate
   "this is the OLD engine's answer" line per SKU, taken once, immutable, and
   directly comparable to the new engine's answer.

   WHAT IT HOLDS

   One row per (channel, sku, match_rank) -- the full ranked candidate list,
   not only the winner, plus the review state that was true when the snapshot
   was taken.

   All three ranks are captured deliberately. The interesting failure is often
   not "the engine picked the wrong product" but "the engine had the right
   product at rank 2 and ranked it second" -- measured on live data, the
   correct row for a Tan Removal listing sat below a turmeric face pack. Only
   the full list can tell those two cases apart, and it is also what shows
   whether a new engine promoted an existing candidate or retrieved something
   the old one never saw at all.

   Run once. Idempotent -- safe to re-run; a second call with the same
   snapshot_label is rejected rather than silently duplicating.
   ============================================================================ */

SET NOCOUNT ON;
GO

IF OBJECT_ID('audit.mapping_baseline', 'U') IS NULL
BEGIN
    CREATE TABLE audit.mapping_baseline
    (
        baseline_id         BIGINT IDENTITY(1,1) NOT NULL,

        -- Which snapshot this row belongs to, e.g. 'pre-v3-engine'. Lets more
        -- than one baseline coexist so a later run can be compared to an
        -- earlier one rather than only to the newest.
        snapshot_label      VARCHAR(100)     NOT NULL,
        captured_at         DATETIME2(3)     NOT NULL CONSTRAINT DF_baseline_captured DEFAULT (SYSUTCDATETIME()),

        channel             VARCHAR(20)      NOT NULL,
        sku                 VARCHAR(200)     NOT NULL,

        -- 1, 2 or 3. A SKU whose retrieval returned nothing is stored once
        -- with match_rank 1 and every old_* column null, so "no candidates"
        -- stays distinguishable from "not snapshotted".
        match_rank          INT              NOT NULL CONSTRAINT DF_baseline_rank DEFAULT (1),

        -- Source-side context, so the snapshot is readable on its own without
        -- joining back to a staging table that may itself have been reloaded.
        title               NVARCHAR(1000)   NULL,
        category            VARCHAR(200)     NULL,
        subcategory         VARCHAR(200)     NULL,
        pack_size           VARCHAR(50)      NULL,
        uom                 VARCHAR(50)      NULL,

        -- The OLD engine's rank-1 answer.
        old_mapping_id      BIGINT           NULL,
        old_product_code    VARCHAR(50)      NULL,
        old_product_name    NVARCHAR(1000)   NULL,
        old_ensemble_score  DECIMAL(8,4)     NULL,
        old_final_score     DECIMAL(8,4)     NULL,
        old_confidence      VARCHAR(50)      NULL,
        old_mapping_status  VARCHAR(50)      NULL,
        old_relationship    VARCHAR(50)      NULL,
        old_llm_reasoning   NVARCHAR(MAX)    NULL,
        old_batch_id        UNIQUEIDENTIFIER NULL,

        -- Individual signals, kept so a later analysis can ask WHICH signal
        -- moved rather than only that the total did.
        old_lexical_score   DECIMAL(8,4)     NULL,
        old_vector_score    DECIMAL(8,4)     NULL,
        old_pack_score      DECIMAL(8,4)     NULL,
        old_category_score  DECIMAL(8,4)     NULL,

        -- The human verdict AS AT snapshot time. This is the ground truth the
        -- comparison is scored against, and it must be frozen here: review
        -- state in staging keeps changing as stewards work, so reading it
        -- later would silently re-baseline the metrics.
        old_review_status   VARCHAR(50)      NULL,
        old_reviewed_by     VARCHAR(200)     NULL,
        old_reviewed_at     DATETIME2(3)     NULL,

        CONSTRAINT PK_mapping_baseline        PRIMARY KEY CLUSTERED (baseline_id),
        -- match_rank is part of the key: one row per candidate, not per SKU.
        CONSTRAINT UQ_mapping_baseline_sku    UNIQUE (snapshot_label, channel, sku, match_rank)
    );

    CREATE INDEX IX_mapping_baseline_label  ON audit.mapping_baseline (snapshot_label);
    CREATE INDEX IX_mapping_baseline_review ON audit.mapping_baseline (snapshot_label, old_review_status);
    -- Most comparisons ask only about the winner; this keeps that path cheap
    -- without forcing the table to hold winners alone.
    CREATE INDEX IX_mapping_baseline_rank1   ON audit.mapping_baseline (snapshot_label, match_rank)
        INCLUDE (channel, sku, old_product_code, old_ensemble_score, old_final_score);

    PRINT 'Created audit.mapping_baseline';
END
ELSE
    PRINT 'audit.mapping_baseline already exists - skipped';
GO

PRINT 'Migration 013 complete.';
GO

SELECT snapshot_label, COUNT(*) AS rows_captured, MIN(captured_at) AS captured_at
FROM   audit.mapping_baseline
GROUP  BY snapshot_label;
GO
