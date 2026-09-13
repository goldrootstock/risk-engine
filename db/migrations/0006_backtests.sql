-- 0006_backtests: backtest results per date, window summaries, and the universe column.
-- Design note 06 §3. risk_runs.universe is promoted from params (note 01 §9-2 rule: the key
-- is filtered on in every backtest and dashboard query now that two sample sets coexist).

ALTER TABLE risk_runs ADD COLUMN universe TEXT NOT NULL DEFAULT 'default';
UPDATE risk_runs SET universe = COALESCE(params->>'universe', 'default');
CREATE INDEX risk_runs_universe_idx ON risk_runs (universe, portfolio_code, tag, as_of_date);
COMMENT ON COLUMN risk_runs.universe IS 'Sample set name from config/universes.toml (promoted from params.universe, 2026-09-13).';

CREATE TABLE backtest_results (
    run_id         BIGINT           PRIMARY KEY REFERENCES risk_runs (run_id) ON DELETE CASCADE,
    universe       TEXT             NOT NULL,
    portfolio_code TEXT             NOT NULL,
    as_of_date     DATE             NOT NULL,   -- t: the run's date
    pnl_date       DATE             NOT NULL,   -- t+1: next aligned observation
    hpl            DOUBLE PRECISION NOT NULL,   -- hypothetical P&L, signed (profit > 0), base currency
    rtpl           DOUBLE PRECISION NOT NULL,   -- risk-theoretical (delta-linear) P&L, signed
    var_99         DOUBLE PRECISION NOT NULL,   -- loss positive
    es_975         DOUBLE PRECISION NOT NULL,
    exception      BOOLEAN          NOT NULL,   -- -hpl > var_99
    attribution    JSONB            NOT NULL DEFAULT '{}'::jsonb,   -- {ticker: loss} on that date
    created_at     TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CHECK (pnl_date > as_of_date)
);
CREATE INDEX backtest_results_series_idx ON backtest_results (universe, portfolio_code, as_of_date);
COMMENT ON TABLE backtest_results IS 'One row per backtested run date. Read-only inputs: risk_runs, risk_measures, prices. Written only by risk_engine.backtest.record.';

CREATE TABLE backtest_summaries (
    summary_id        BIGINT           GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    universe          TEXT             NOT NULL,
    portfolio_code    TEXT             NOT NULL,
    window_start      DATE             NOT NULL,
    window_end        DATE             NOT NULL,
    n_obs             INTEGER          NOT NULL,
    exceptions        INTEGER          NOT NULL,
    expected          DOUBLE PRECISION NOT NULL,
    kupiec_lr         DOUBLE PRECISION,
    kupiec_p          DOUBLE PRECISION,
    christoffersen_lr DOUBLE PRECISION,
    christoffersen_p  DOUBLE PRECISION,
    cc_lr             DOUBLE PRECISION,
    cc_p              DOUBLE PRECISION,
    traffic_light     TEXT             NOT NULL CHECK (traffic_light IN ('green', 'yellow', 'red')),
    pla_spearman      DOUBLE PRECISION,
    pla_ks            DOUBLE PRECISION,
    pla_zone          TEXT             CHECK (pla_zone IN ('green', 'amber', 'red')),
    params            JSONB            NOT NULL DEFAULT '{}'::jsonb,
    code_version      TEXT,
    created_at        TIMESTAMPTZ      NOT NULL DEFAULT now()
);
CREATE INDEX backtest_summaries_series_idx ON backtest_summaries (universe, portfolio_code, window_end);
COMMENT ON TABLE backtest_summaries IS 'Window statistics (Kupiec, Christoffersen, Basel zone, PLA) for a run series. A re-run with other parameters is a new row with a different params sha256, never an update.';
