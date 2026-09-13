-- 0005_rates_to_fred: move the 11 constant-maturity Treasury series from the Treasury CSV
-- endpoint to FRED (DGS1MO ... DGS30), keeping instrument_id so that the 96,145 loaded
-- rows in prices stay attached. Data migration only; no DDL. Design note 03 §16.
--
-- Why: home.treasury.gov sits behind a WAF that only answers browser-format User-Agents.
-- The workaround worked but does not belong in a public repository. FRED serves the same
-- H.15 series through a documented API with a free key; values matched on every
-- overlapping date [확인 2026-09-13: 11 series, 0 mismatches, docs/decisions.md].
-- etl_runs history keeps source = 'ustreasury' for the loads made before this change.

UPDATE instruments
SET source = 'fred',
    source_id = CASE ticker
        WHEN 'UST_1M'  THEN 'DGS1MO'
        WHEN 'UST_3M'  THEN 'DGS3MO'
        WHEN 'UST_6M'  THEN 'DGS6MO'
        WHEN 'UST_1Y'  THEN 'DGS1'
        WHEN 'UST_2Y'  THEN 'DGS2'
        WHEN 'UST_3Y'  THEN 'DGS3'
        WHEN 'UST_5Y'  THEN 'DGS5'
        WHEN 'UST_7Y'  THEN 'DGS7'
        WHEN 'UST_10Y' THEN 'DGS10'
        WHEN 'UST_20Y' THEN 'DGS20'
        WHEN 'UST_30Y' THEN 'DGS30'
    END,
    name = replace(name, 'U.S. Treasury par yield', 'U.S. Treasury constant-maturity yield (H.15 via FRED)'),
    updated_at = now()
WHERE source = 'ustreasury';
