-- 0003_instrument_vocab: vocabulary needed by universe v1 (design note 02 §3-1, §11).
--
-- 1. instrument_type gains 'yield_curve' (constant-maturity par yields) and 'commodity_spot'
--    (physical spot assessments). 'index' stays for equity/other index levels.
-- 2. source_id records the vendor's own identifier for the series (a CSV column name for
--    Treasury, an ISO code for ECB, an EIA series id) so that the ETL is data-driven.
-- 3. return_type fixes how the return builder differences the series: log returns for
--    strictly positive prices; absolute changes (bp for yields, currency units for spot
--    prices that can go negative, e.g. WTI 2020-04-20).
-- 4. v_risk_headline guards the fraction-of-NAV columns against portfolio_value = 0
--    (a futures-only or fully hedged book can have zero market value): NULL, not an error.

ALTER TABLE instruments DROP CONSTRAINT instruments_instrument_type_check;
ALTER TABLE instruments ADD CONSTRAINT instruments_instrument_type_check
    CHECK (instrument_type IN ('stock', 'etf', 'future', 'fx_spot', 'index', 'yield_curve', 'commodity_spot'));

ALTER TABLE instruments ADD COLUMN source_id TEXT NOT NULL DEFAULT '';
ALTER TABLE instruments ALTER COLUMN source_id DROP DEFAULT;

ALTER TABLE instruments ADD COLUMN return_type TEXT NOT NULL DEFAULT 'log'
    CHECK (return_type IN ('log', 'absolute'));

COMMENT ON COLUMN instruments.source_id   IS 'Vendor identifier of the series (Treasury CSV column, ECB ISO code, EIA series id). Used by the ETL fetcher.';
COMMENT ON COLUMN instruments.return_type IS 'log = ln(P_t/P_{t-1}); absolute = P_t - P_{t-1} (bp for yields). Absolute is required where prices can be <= 0.';

CREATE OR REPLACE VIEW v_risk_headline AS
SELECT r.run_id,
       r.portfolio_code,
       r.as_of_date,
       r.method,
       r.tag,
       r.base_currency,
       r.portfolio_value,
       MAX(m.value) FILTER (WHERE m.measure = 'var' AND m.confidence = 0.99)  AS var_99,
       MAX(m.value) FILTER (WHERE m.measure = 'es'  AND m.confidence = 0.975) AS es_975,
       MAX(m.value) FILTER (WHERE m.measure = 'var' AND m.confidence = 0.99)
           / NULLIF(r.portfolio_value, 0) AS var_99_frac,
       MAX(m.value) FILTER (WHERE m.measure = 'es'  AND m.confidence = 0.975)
           / NULLIF(r.portfolio_value, 0) AS es_975_frac
FROM risk_runs r
JOIN risk_measures m USING (run_id)
WHERE m.scope_type = 'portfolio'
GROUP BY r.run_id;

COMMENT ON VIEW v_risk_headline IS 'Headline VaR 99 / ES 97.5 per run, in base currency and as a fraction of portfolio_value (NULL when portfolio_value = 0).';
