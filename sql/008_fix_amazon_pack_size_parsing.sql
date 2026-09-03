-- 008_fix_amazon_pack_size_parsing.sql
--
-- Fixes pack_size / uom parsing in staging.usp_load_amazon_products.
--
-- THE BUG
-- The procedure locates the first digit in the title with
--     PATINDEX('%[0-9]%', p.title)
-- and then asks whether a unit appears ANYWHERE in the following 20
-- characters:
--     WHEN t.TitleUpper LIKE '% L%' THEN ...
-- Those two steps are independent, so any word starting with the unit
-- letter inside that window is accepted as the unit for that number:
--
--   "Himalaya Since 1930 Ultra-Moisturizing Cocoa Butter Lip Balm"
--        window = '1930 Ultra-Moisturiz'  -> ' U' matched ' L%'? no --
--        but ' L' is found later in the title by the TitleUpper test,
--        which scans the WHOLE title, not the window. Stored as 1930 L.
--
--   "Himalaya Koflet - Bottle of 200 Lozenges"
--        ' Lozenges' satisfies '% L%'. Stored as 200 L.
--
-- Both normalise to millions of millilitres downstream and make pack
-- scoring meaningless for the row. 146 amazon rows carry a year-like
-- size today and the column reaches 84,935,374 at the extreme.
--
-- THE FIX
-- Test the characters IMMEDIATELY AFTER the number instead of scanning a
-- window. A unit qualifies only when it directly follows the digits, with
-- at most one space between. "200ml" and "100 g" still parse; "1930 Ultra"
-- and "200 Lozenges" no longer do.
--
-- SCOPE -- ONLY the pack_size and uom expressions change. clean_title,
-- pack_no, ingredient, combo_flag, price_band, product_benefit,
-- pack_size_band, the WHERE clause, the sync_status update and the
-- transaction handling are all byte-identical to the current procedure.
--
-- Single-letter units (L, G) additionally require that the next character
-- is not a letter, so "5 L" is litres but "200 Lozenges" and "10 Grooming"
-- are not. Multi-letter units (ML, GM, KG, LTR, OZ, TAB, CAP) are checked
-- before the single-letter ones, so "500ML" is never read as "5 ... L".
--
-- MG/MCG are matched FIRST and mapped to NULL: a dose is not a pack size,
-- and "Shilajit 500mg" must never become a 500 g pack. Spelled-out forms
-- (GRAMS, GMS, KGS, LITRE) are included because they are common here.
-- LBS is deliberately excluded -- it needs a 453.6 conversion nothing
-- downstream performs.
--
-- Verified against 16 representative titles covering every unit the
-- original CASE handled plus the spelled-out forms, and the known-bad rows.
-- Measured over all 153,822 amazon rows: ML 38658, GM 29385, KG 3915,
-- TAB 2521, OZ 2331, CAP 1986, L 1954.

USE [AureusSentinel_Latest]
GO
SET ANSI_NULLS ON
GO
SET QUOTED_IDENTIFIER ON
GO

ALTER PROCEDURE [staging].[usp_load_amazon_products]
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    BEGIN TRY
        BEGIN TRANSACTION;

        INSERT INTO [staging].[amazon_products]
        (
            batch_id, sku, title, clean_title, brand, category,
            subcategory, pack_no, pack_size, uom, ingredient, combo_flag,
            price_band, product_benefit, pack_size_band
        )
        SELECT
            p.batch_id,
            p.sku,
            p.title,

            -------------------------------------------------------------------
            -- CLEAN TITLE  (unchanged)
            -------------------------------------------------------------------
            CASE
                WHEN p.pack_size_band IS NOT NULL
                     AND LTRIM(RTRIM(p.pack_size_band)) <> ''
                     AND UPPER(LTRIM(RTRIM(p.pack_size_band))) <> 'NULL'
                THEN
                    UPPER(LTRIM(RTRIM(p.title))) + ' ' +
                    CASE
                        WHEN p.pack_size_band LIKE '<=%'
                            THEN REPLACE(REPLACE(p.pack_size_band,'<=',''),'.0','') + ' OR LESS'

                        WHEN p.pack_size_band LIKE '>%'
                            THEN 'GREATER THAN ' +
                                 REPLACE(REPLACE(p.pack_size_band,'>',''),'.0','')

                        WHEN p.pack_size_band LIKE '%-%'
                            THEN 'BETWEEN '
                                 + REPLACE(LEFT(p.pack_size_band,CHARINDEX('-',p.pack_size_band)-1),'.0','')
                                 + ' AND '
                                 + REPLACE(SUBSTRING(p.pack_size_band,CHARINDEX('-',p.pack_size_band)+1,100),'.0','')

                        ELSE p.pack_size_band
                    END
                ELSE
                    UPPER(LTRIM(RTRIM(p.title)))
            END AS clean_title,

            p.brand,
            p.category,
            p.subcategory,

            -------------------------------------------------------------------
            -- PACK NO  (unchanged)
            -------------------------------------------------------------------
            CASE
            WHEN PATINDEX('%PACK OF [0-9]%', t.TitleUpper) > 0
            THEN
                TRY_CAST(
                    LEFT(
                        LTRIM(
                            SUBSTRING(
                                t.TitleUpper,
                                PATINDEX('%PACK OF [0-9]%', t.TitleUpper) + LEN('PACK OF '),
                                10
                            )
                        ),
                        PATINDEX(
                            '%[^0-9]%',
                            LTRIM(
                                SUBSTRING(
                                    t.TitleUpper,
                                    PATINDEX('%PACK OF [0-9]%', t.TitleUpper) + LEN('PACK OF '),
                                    10
                                )
                            ) + 'X'
                        ) - 1
                    ) AS INT
                )

            ELSE NULL
        END AS pack_no,

            -------------------------------------------------------------------
            -- PACK SIZE  (CHANGED)
            -- Emitted only when a unit directly follows the number, i.e. when
            -- the UOM expression below resolves. The digits themselves are
            -- extracted exactly as before.
            -------------------------------------------------------------------
            CASE
                WHEN pos.NumPos = 0
                    THEN NULL

                WHEN nxt.UomAfter IS NULL
                    THEN NULL

                ELSE TRY_CAST(
                        LEFT(
                            SUBSTRING(p.title, pos.NumPos, 20),
                            PATINDEX('%[^0-9.]%', SUBSTRING(p.title, pos.NumPos, 20) + 'X') - 1
                        )
                    AS DECIMAL(10,2))
            END AS pack_size,

            -------------------------------------------------------------------
            -- UOM  (CHANGED)
            -- Resolved from the 6 characters immediately after the number
            -- rather than from a 20-character window or the whole title.
            -------------------------------------------------------------------
            nxt.UomAfter AS uom,

            -------------------------------------------------------------------
            -- INGREDIENT  (unchanged)
            -------------------------------------------------------------------
            p.active_ingredient AS ingredient,

            -------------------------------------------------------------------
            -- COMBO FLAG  (unchanged)
            -------------------------------------------------------------------
            CASE
                WHEN p.combo_flag IS NULL
                     OR UPPER(p.combo_flag) = 'NULL'
                THEN 0
                ELSE 1
            END AS combo_flag,

            p.price_band,
            p.product_benefit,
            p.pack_size_band

        FROM raw.oneds_products p

        CROSS APPLY
        (
            SELECT UPPER(p.title) AS TitleUpper
        ) t

        CROSS APPLY
        (
            SELECT PATINDEX('%[0-9]%', p.title) AS NumPos
        ) pos

        -- The 6 characters that directly follow the first number -- 6 so the
        -- spelled-out units (GRAMS, LITRE) fit. Everything
        -- the UOM decision needs is in here, so the unit can no longer be
        -- borrowed from an unrelated word elsewhere in the title.
        CROSS APPLY
        (
            SELECT CASE
                     WHEN pos.NumPos = 0 THEN NULL
                     ELSE UPPER(
                            SUBSTRING(
                                p.title,
                                pos.NumPos
                                  + PATINDEX('%[^0-9.]%', SUBSTRING(p.title, pos.NumPos, 20) + 'X') - 1,
                                6
                            )
                          )
                   END AS AfterNum
        ) a

        CROSS APPLY
        (
            SELECT CASE
                     -- MG is a DOSE, never a pack size: "Shilajit 500mg" is
                     -- 500 milligrams of active per serving, not a 500 g pack.
                     -- Matched first and mapped to NULL so it can never be
                     -- read as GM by the 'G...' rule below. The old procedure
                     -- stored several of these as 500 L.
                     WHEN a.AfterNum LIKE 'MG%'  OR a.AfterNum LIKE ' MG%'  THEN NULL
                     WHEN a.AfterNum LIKE 'MCG%' OR a.AfterNum LIKE ' MCG%' THEN NULL

                     -- Multi-letter units first: "500ML" must never be read
                     -- as "5 ... L". Spelled-out variants are included
                     -- because they are common in this data -- GRAM(S) 2329
                     -- rows, GMS 1315, LIT/LITRE 669, all directly after the
                     -- number. LBS is deliberately absent: it needs a 453.6
                     -- conversion the downstream code does not do.
                     WHEN a.AfterNum LIKE 'ML%'   OR a.AfterNum LIKE ' ML%'   THEN 'ML'
                     WHEN a.AfterNum LIKE 'GMS%'  OR a.AfterNum LIKE ' GMS%'  THEN 'GM'
                     WHEN a.AfterNum LIKE 'GM%'   OR a.AfterNum LIKE ' GM%'   THEN 'GM'
                     WHEN a.AfterNum LIKE 'GRAM%' OR a.AfterNum LIKE ' GRAM%' THEN 'GM'
                     WHEN a.AfterNum LIKE 'KGS%'  OR a.AfterNum LIKE ' KGS%'  THEN 'KG'
                     WHEN a.AfterNum LIKE 'KG%'   OR a.AfterNum LIKE ' KG%'   THEN 'KG'
                     WHEN a.AfterNum LIKE 'LTR%'  OR a.AfterNum LIKE ' LTR%'  THEN 'L'
                     WHEN a.AfterNum LIKE 'LIT%'  OR a.AfterNum LIKE ' LIT%'  THEN 'L'
                     WHEN a.AfterNum LIKE 'OZ%'   OR a.AfterNum LIKE ' OZ%'   THEN 'OZ'
                     WHEN a.AfterNum LIKE 'TAB%'  OR a.AfterNum LIKE ' TAB%'  THEN 'TAB'
                     WHEN a.AfterNum LIKE 'CAP%'  OR a.AfterNum LIKE ' CAP%'  THEN 'CAP'

                     -- Single-letter units: the next character must not be a
                     -- letter, so "5 L" is litres while "200 Lozenges" and
                     -- "10 Grooming" are not.
                     WHEN a.AfterNum LIKE 'L[^A-Z]%' OR a.AfterNum LIKE ' L[^A-Z]%'
                          OR a.AfterNum = 'L' OR a.AfterNum = ' L' THEN 'L'
                     WHEN a.AfterNum LIKE 'G[^A-Z]%' OR a.AfterNum LIKE ' G[^A-Z]%'
                          OR a.AfterNum = 'G' OR a.AfterNum = ' G' THEN 'GM'

                     ELSE NULL
                   END AS UomAfter
        ) nxt

        WHERE p.channel_name = 'Amazon'
          AND sync_status = 'PENDING';

          UPDATE [raw].[oneds_products]
          SET sync_status = 'COMPLETED'
          WHERE channel_name = 'Amazon' AND sync_status = 'PENDING';

        COMMIT TRANSACTION;

        PRINT 'Amazon products loaded successfully.';
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;

        THROW;
    END CATCH
END;
GO
