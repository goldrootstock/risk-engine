-- 0004_etl_runs: one row per (sync run, series) — what was fetched, validated and loaded.
-- Design note 03 §12. Naming follows risk_runs: *_at timestamps, code_version, JSONB fields.
--
-- Written only by the sync orchestrator. validate() cannot write here: it never receives a
-- connection (design note 00, "checks are read-only").

CREATE TABLE etl_runs (
    etl_run_id     BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL,
    finished_at    TIMESTAMPTZ NOT NULL,
    source         TEXT        NOT NULL,
    source_id      TEXT        NOT NULL,
    instrument_id  BIGINT      REFERENCES instruments (instrument_id),   -- NULL when the series is not in the universe
    status         TEXT        NOT NULL CHECK (status IN ('loaded', 'skipped', 'failed', 'dry_run')),
    rows_fetched   INTEGER     NOT NULL DEFAULT 0,
    rows_inserted  INTEGER     NOT NULL DEFAULT 0,
    rows_updated   INTEGER     NOT NULL DEFAULT 0,   -- vendor restatements of already-loaded dates
    rows_unchanged INTEGER     NOT NULL DEFAULT 0,
    first_date     DATE,
    last_date      DATE,
    cache_sha256   TEXT,                             -- RawFile that produced the frame (manifest key)
    findings       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    params         JSONB       NOT NULL DEFAULT '{}'::jsonb,   -- since, offline, thresholds + validation.toml sha256
    code_version   TEXT,
    CHECK (finished_at >= started_at)
);

CREATE INDEX etl_runs_source_series_idx ON etl_runs (source, source_id, started_at);
CREATE INDEX etl_runs_status_idx        ON etl_runs (status, started_at);

COMMENT ON TABLE  etl_runs          IS 'ETL execution record per series. Answers "what was loaded, what was skipped and why" for the model document data-quality section.';
COMMENT ON COLUMN etl_runs.status   IS 'loaded = upserted; skipped = validation error, nothing written; failed = fetch/parse/DB exception; dry_run = --dry-run, nothing written.';
COMMENT ON COLUMN etl_runs.findings IS 'JSON array of {level, code, detail, price_date, value, threshold}; codes are the FindingCode vocabulary in risk_engine.data.etl.contract.';
COMMENT ON COLUMN etl_runs.params   IS 'Run inputs: since, offline, dry_run, thresholds used and the sha256 of config/validation.toml.';
