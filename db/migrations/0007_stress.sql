-- 0007_stress: stress runs and their per-scenario results (design note 07 §5).

CREATE TABLE stress_runs (
    stress_run_id       BIGINT           GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    portfolio_code      TEXT             NOT NULL,
    universe            TEXT             NOT NULL,
    as_of_date          DATE             NOT NULL,
    positions_as_of     DATE             NOT NULL,
    portfolio_value     DOUBLE PRECISION NOT NULL,
    es_975              DOUBLE PRECISION NOT NULL,   -- FHS ES on as_of_date, reference for loss_over_es
    scenario_set_sha256 TEXT             NOT NULL,   -- config/stress_scenarios.toml
    params              JSONB            NOT NULL DEFAULT '{}'::jsonb,
    code_version        TEXT,
    created_at          TIMESTAMPTZ      NOT NULL DEFAULT now()
);
COMMENT ON TABLE stress_runs IS 'One stress evaluation of a book on a date. Shock sizes come only from the scenario file whose sha256 is recorded here.';

CREATE TABLE stress_results (
    stress_run_id  BIGINT           NOT NULL REFERENCES stress_runs (stress_run_id) ON DELETE CASCADE,
    scenario       TEXT             NOT NULL,
    kind           TEXT             NOT NULL CHECK (kind IN ('historical', 'hypothetical', 'correlation', 'vol_lag')),
    window_start   DATE,
    window_end     DATE,
    loss           DOUBLE PRECISION NOT NULL,   -- positive = loss, base currency
    worst_day      DATE,
    worst_day_loss DOUBLE PRECISION,
    loss_over_es   DOUBLE PRECISION,            -- loss / stress_runs.es_975
    attribution    JSONB            NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (stress_run_id, scenario)
);
COMMENT ON TABLE stress_results IS 'Loss per scenario. historical = unscaled cumulative change over the window; hypothetical = file shocks; correlation = joint / independent / undiversified ES; vol_lag = worst historical day over today''s ES.';
