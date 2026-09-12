-- 0002_risk_runs: run header + normalised result rows + measure catalogue.
-- Design note: docs/design/01-data-layer-schema.md §8 (decision 3, option B, approved 2026-09-12).
--
-- Units: every measure has exactly one unit, defined once in risk_measure_types. Values whose
-- unit is 'currency' are in risk_runs.base_currency. Fractions of NAV are never stored: derive
-- them as value / portfolio_value (see v_risk_headline).

CREATE TABLE risk_measure_types (
    measure     TEXT PRIMARY KEY,
    unit        TEXT NOT NULL CHECK (unit IN ('currency', 'currency_per_pct', 'currency_per_bp')),
    description TEXT NOT NULL
);

COMMENT ON TABLE  risk_measure_types      IS 'Vocabulary of result measures. Mirrored by risk_engine.data.vocab.Measure (tested for equality).';
COMMENT ON COLUMN risk_measure_types.unit IS 'currency = amount in run base currency; currency_per_pct = P&L per +1% factor move; currency_per_bp = P&L per +1bp rate move.';

INSERT INTO risk_measure_types (measure, unit, description) VALUES
    ('var',                   'currency',         'Value at Risk at `confidence`, `horizon_days`. Loss positive.'),
    ('es',                    'currency',         'Expected Shortfall at `confidence`. Loss positive.'),
    ('stressed_es',           'currency',         'ES over the stressed 250-day window (FRTB SES style).'),
    ('component_es',          'currency',         'Euler contribution to portfolio ES; scope = instrument or asset_class. Sums to es.'),
    ('incremental_var',       'currency',         'VaR(portfolio) - VaR(portfolio without scope); scope = instrument.'),
    ('factor_exposure',       'currency_per_pct', 'Sensitivity to a +1% move of the factor; scope = factor.'),
    ('factor_dv01',           'currency_per_bp',  'Sensitivity to a +1bp parallel move of the rates factor; scope = factor.'),
    ('im',                    'currency',         'Initial margin, total (core + add-ons after floors).'),
    ('im_core',               'currency',         'FHS ES-based core margin before floors and add-ons.'),
    ('im_floor',              'currency',         'Amount added by the anti-procyclicality floor.'),
    ('im_stress_blend',       'currency',         'Amount added by the stressed-period blend.'),
    ('im_liquidity_addon',    'currency',         'Liquidity add-on.'),
    ('im_concentration_addon','currency',         'Concentration add-on.'),
    ('im_span_legacy',        'currency',         'Legacy SPAN 16-scenario margin for comparison.');

CREATE TABLE risk_runs (
    run_id          BIGINT           GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    portfolio_code  TEXT             NOT NULL,
    as_of_date      DATE             NOT NULL,
    positions_as_of DATE             NOT NULL,
    base_currency   CHAR(3)          NOT NULL DEFAULT 'USD',
    method          TEXT             NOT NULL CHECK (method IN ('fhs', 'parametric', 'mc')),
    horizon_days    SMALLINT         NOT NULL DEFAULT 1 CHECK (horizon_days >= 1),
    window_days     INTEGER          NOT NULL CHECK (window_days > 0),
    n_scenarios     INTEGER          NOT NULL CHECK (n_scenarios > 0),
    portfolio_value DOUBLE PRECISION NOT NULL,
    params          JSONB            NOT NULL DEFAULT '{}'::jsonb,
    tag             TEXT             NOT NULL DEFAULT 'adhoc',
    code_version    TEXT,
    created_at      TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CHECK (positions_as_of <= as_of_date)
);

CREATE INDEX risk_runs_portfolio_tag_date_idx ON risk_runs (portfolio_code, tag, as_of_date);

COMMENT ON TABLE  risk_runs                 IS 'One row per engine run: inputs and configuration only. Results live in risk_measures.';
COMMENT ON COLUMN risk_runs.as_of_date      IS 'Valuation date: prices up to and including this date.';
COMMENT ON COLUMN risk_runs.positions_as_of IS 'Snapshot actually used: latest positions.as_of_date <= as_of_date.';
COMMENT ON COLUMN risk_runs.base_currency   IS 'Currency of every ''currency''-unit value in risk_measures for this run, and of portfolio_value.';
COMMENT ON COLUMN risk_runs.portfolio_value IS 'Market value on as_of_date in base_currency. Denominator for fraction-of-NAV reporting.';
COMMENT ON COLUMN risk_runs.params          IS 'Method-specific settings (EWMA lambda, GARCH spec, MC paths, seed). Promote a key to a column once it appears in a WHERE clause twice.';
COMMENT ON COLUMN risk_runs.tag             IS 'daily_batch = official series used by backtests; adhoc / experiment otherwise.';

CREATE TABLE risk_measures (
    run_id     BIGINT           NOT NULL REFERENCES risk_runs (run_id) ON DELETE CASCADE,
    measure    TEXT             NOT NULL REFERENCES risk_measure_types (measure),
    confidence NUMERIC(5,4)     CHECK (confidence > 0 AND confidence < 1),
    scope_type TEXT             NOT NULL DEFAULT 'portfolio'
                                CHECK (scope_type IN ('portfolio', 'asset_class', 'instrument', 'factor')),
    scope_key  TEXT             NOT NULL DEFAULT '',
    value      DOUBLE PRECISION NOT NULL,
    UNIQUE NULLS NOT DISTINCT (run_id, measure, confidence, scope_type, scope_key)
);

CREATE INDEX risk_measures_series_idx
    ON risk_measures (measure, scope_type, scope_key, confidence, run_id) INCLUDE (value);

COMMENT ON TABLE  risk_measures            IS 'One result value per row. Unit comes from risk_measure_types; currency from risk_runs.base_currency. Losses positive.';
COMMENT ON COLUMN risk_measures.confidence IS '0.9900 / 0.9750 for tail measures; NULL for measures without a confidence level.';
COMMENT ON COLUMN risk_measures.scope_key  IS 'asset_class name, instrument_id as text, or factor name. Empty string when scope_type = portfolio.';

CREATE VIEW v_risk_headline AS
SELECT r.run_id,
       r.portfolio_code,
       r.as_of_date,
       r.method,
       r.tag,
       r.base_currency,
       r.portfolio_value,
       MAX(m.value) FILTER (WHERE m.measure = 'var' AND m.confidence = 0.99)  AS var_99,
       MAX(m.value) FILTER (WHERE m.measure = 'es'  AND m.confidence = 0.975) AS es_975,
       MAX(m.value) FILTER (WHERE m.measure = 'var' AND m.confidence = 0.99)  / r.portfolio_value AS var_99_frac,
       MAX(m.value) FILTER (WHERE m.measure = 'es'  AND m.confidence = 0.975) / r.portfolio_value AS es_975_frac
FROM risk_runs r
JOIN risk_measures m USING (run_id)
WHERE m.scope_type = 'portfolio'
GROUP BY r.run_id;

COMMENT ON VIEW v_risk_headline IS 'Headline VaR 99 / ES 97.5 per run, in base currency and as a fraction of portfolio_value.';
