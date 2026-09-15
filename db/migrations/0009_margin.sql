-- 0009_margin: margin coverage backtest, Cover-N default fund; COMMENT corrections from the
-- 2026-09-15 audit (design note 01 §10-5). Design note 09 §8 (JK approval 4, 2026-09-15).
-- No existing table or constraint is changed. Margin runs themselves live in risk_runs
-- (tag = 'margin_batch', horizon_days = 2) and risk_measures (the im_* catalogue rows of 0002).

COMMENT ON COLUMN risk_runs.tag IS
  'daily_batch = official 1-day series for the risk backtest; margin_batch = official 2-day MPOR series for the margin coverage backtest; adhoc / experiment / margin_adhoc otherwise. Readers select (tag, method, horizon_days) positively (note 08 §3).';
COMMENT ON COLUMN instruments.multiplier IS
  'Contract multiplier m. P&L per note 04 §4: log kind q*m*S*(exp(x)-1); abs kind q*m*x; bp kind -DV01*x with DV01 = q*m*D_mod*1e-4.';
COMMENT ON COLUMN instruments.quote_type IS
  'price = level in the quote currency; yield = percent. How the series is differenced is return_type (0003), not quote_type.';

CREATE TABLE margin_coverage_results (
    run_id           BIGINT           PRIMARY KEY REFERENCES risk_runs (run_id) ON DELETE CASCADE,
    universe         TEXT             NOT NULL,
    portfolio_code   TEXT             NOT NULL,
    as_of_date       DATE             NOT NULL,   -- t: the margin run's date
    pnl_date         DATE             NOT NULL,   -- t+2: second aligned observation after t
    h_business_days  INTEGER          NOT NULL,   -- business days t -> pnl_date (2 unless a gap)
    realised_loss    DOUBLE PRECISION NOT NULL,   -- loss positive, base currency, positions fixed at t
    im               DOUBLE PRECISION NOT NULL,   -- official IM on t (measure 'im')
    im_core          DOUBLE PRECISION NOT NULL,
    im_span_legacy   DOUBLE PRECISION,
    im_h_block       DOUBLE PRECISION,            -- IM recomputed at horizon h (NULL when h = MPOR)
    breach           BOOLEAN          NOT NULL,   -- official: realised_loss > (im_h_block if h > MPOR else im)
    breach_raw       BOOLEAN          NOT NULL,   -- realised_loss > im (MPOR IM regardless of h)
    breach_core      BOOLEAN          NOT NULL,   -- realised_loss > im_core
    breach_span      BOOLEAN,                     -- realised_loss > im_span_legacy
    shortfall        DOUBLE PRECISION NOT NULL,   -- max(0, realised_loss - im)
    attribution      JSONB            NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CHECK (pnl_date > as_of_date)
);
CREATE INDEX margin_coverage_results_series_idx
    ON margin_coverage_results (universe, portfolio_code, as_of_date);
COMMENT ON TABLE margin_coverage_results IS 'One row per margin run date: realised MPOR loss vs the IM recorded on that date. Read-only inputs (risk_runs, risk_measures, prices); written only by risk_engine.margin.coverage.record.';

CREATE TABLE margin_coverage_summaries (
    summary_id            BIGINT           GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    universe              TEXT             NOT NULL,
    portfolio_code        TEXT             NOT NULL,
    window_start          DATE             NOT NULL,
    window_end            DATE             NOT NULL,
    n_obs                 INTEGER          NOT NULL,
    breaches              INTEGER          NOT NULL,
    breaches_raw          INTEGER          NOT NULL,
    breaches_core         INTEGER          NOT NULL,
    breaches_span         INTEGER,
    coverage              DOUBLE PRECISION NOT NULL,   -- 1 - breaches / n_obs
    target                DOUBLE PRECISION NOT NULL,   -- 0.99 (CPMI-IOSCO)
    kupiec_lr             DOUBLE PRECISION,
    kupiec_p              DOUBLE PRECISION,
    max_shortfall         DOUBLE PRECISION NOT NULL,
    max_shortfall_over_im DOUBLE PRECISION,
    params                JSONB            NOT NULL DEFAULT '{}'::jsonb,   -- margin_params sha256, coverage cfg
    code_version          TEXT,
    created_at            TIMESTAMPTZ      NOT NULL DEFAULT now()
);
CREATE INDEX margin_coverage_summaries_series_idx
    ON margin_coverage_summaries (universe, portfolio_code, window_end);
COMMENT ON TABLE margin_coverage_summaries IS 'Window statistics of the margin coverage backtest. A re-run is a new row with a different params sha256, never an update.';

CREATE TABLE default_fund_runs (
    default_fund_run_id  BIGINT           GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    universe             TEXT             NOT NULL,
    as_of_date           DATE             NOT NULL,
    scenario_set_sha256  TEXT             NOT NULL,   -- config/stress_scenarios.toml
    margin_params_sha256 TEXT             NOT NULL,   -- config/margin_params.toml
    n_members            INTEGER          NOT NULL,
    cover                INTEGER          NOT NULL,   -- 2
    default_fund         DOUBLE PRECISION NOT NULL,   -- max over scenarios of the top-`cover` uncovered losses
    binding_scenario     TEXT             NOT NULL,
    binding_members      TEXT[]           NOT NULL,
    params               JSONB            NOT NULL DEFAULT '{}'::jsonb,
    code_version         TEXT,
    created_at           TIMESTAMPTZ      NOT NULL DEFAULT now()
);
COMMENT ON TABLE default_fund_runs IS 'One Cover-N sizing of the default fund on a date: members are portfolio_code values, shocks come only from the scenario file (sha256 recorded).';

CREATE TABLE default_fund_results (
    default_fund_run_id BIGINT           NOT NULL REFERENCES default_fund_runs (default_fund_run_id) ON DELETE CASCADE,
    scenario            TEXT             NOT NULL,
    kind                TEXT             NOT NULL CHECK (kind IN ('historical', 'hypothetical')),
    portfolio_code      TEXT             NOT NULL,   -- the clearing member
    im_run_id           BIGINT           NOT NULL REFERENCES risk_runs (run_id),   -- margin run supplying im
    stress_loss         DOUBLE PRECISION NOT NULL,   -- loss positive
    im                  DOUBLE PRECISION NOT NULL,
    uncovered           DOUBLE PRECISION NOT NULL,   -- max(0, stress_loss - im)
    UNIQUE (default_fund_run_id, scenario, portfolio_code)
);
COMMENT ON TABLE default_fund_results IS 'Per (scenario, member): stress loss, the IM recorded for that member on the date, and the uncovered excess that the default fund must absorb.';
