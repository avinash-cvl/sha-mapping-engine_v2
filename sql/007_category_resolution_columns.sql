-- 007_category_resolution_columns.sql
--
-- Adds the V2 category-resolution decision to every channel's mapping table.
--
-- The pipeline records this decision in llm_reasoning as a prefix tag
-- (e.g. "[master:FACE WASH/FACE WASH conf=0.94] ...") whether or not these
-- columns exist, so applying this script is optional -- it turns that tag
-- into queryable columns. common/db.py::_mapping_row() detects the columns
-- via SQLAlchemy reflection and populates them only where present, so this
-- can be applied to one channel at a time.
--
-- Every column is NULLable with no default: existing rows and any INSERT
-- that omits them are unaffected, so this is safe to run against live tables
-- and is backward compatible with oneds_competitor, which never sets them.
--
-- IMPORTANT: reflect_metadata() memoizes per process. Apply this BETWEEN
-- runs -- a table altered mid-run stays invisible for the rest of that run
-- and the columns silently stay NULL.
--
-- The canonical home for this DDL is the schema repo alongside
-- 006_channel_schema.sql; it lives here because that repo is not vendored
-- into sha-pipelines. Keep the two in sync.

SET NOCOUNT ON;
GO

DECLARE @sql NVARCHAR(MAX) = N'';

-- Driven off config.channels rather than a hardcoded channel list, so a new
-- channel following the same naming convention is picked up automatically.
SELECT @sql = @sql + N'
ALTER TABLE ' + c.staging_mapping_table_name + N' ADD
    master_category      VARCHAR(100)  NULL,
    master_subcategory   VARCHAR(100)  NULL,
    category_confidence  DECIMAL(5, 4) NULL,
    category_evidence    VARCHAR(500)  NULL;
'
FROM config.channels AS c
WHERE c.is_active = 1
  AND NOT EXISTS (
        SELECT 1
        FROM sys.columns
        WHERE object_id = OBJECT_ID(c.staging_mapping_table_name)
          AND name = 'master_category'
  );

IF @sql = N''
    PRINT 'No active channel mapping tables need these columns.';
ELSE
BEGIN
    PRINT @sql;
    EXEC sp_executesql @sql;
END
GO

-- Verification: expect four rows per active channel.
SELECT
    t.name  AS table_name,
    c.name  AS column_name,
    ty.name AS data_type,
    c.is_nullable
FROM sys.columns AS c
JOIN sys.tables  AS t  ON t.object_id = c.object_id
JOIN sys.types   AS ty ON ty.user_type_id = c.user_type_id
WHERE c.name IN (
        'master_category',
        'master_subcategory',
        'category_confidence',
        'category_evidence'
      )
ORDER BY t.name, c.column_id;
GO
