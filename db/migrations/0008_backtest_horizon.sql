-- 0008_backtest_horizon: horizon-consistent exception test (JK decision 2026-09-13).
-- The official exception compares the multi-business-day P&L with an h-day FHS VaR built
-- by block bootstrap of consecutive residual vectors; raw (1-day VaR) and sqrt(h) are kept
-- for cross-checking. Model document §7-2.

ALTER TABLE backtest_results
    ADD COLUMN h_business_days INTEGER          NOT NULL DEFAULT 1,
    ADD COLUMN var_h_block     DOUBLE PRECISION,           -- h-day block-bootstrap VaR (NULL when h = 1: equals var_99)
    ADD COLUMN var_h_sqrt      DOUBLE PRECISION,           -- var_99 * sqrt(h)
    ADD COLUMN exception_raw   BOOLEAN          NOT NULL DEFAULT FALSE,   -- -hpl > var_99 (1-day VaR)
    ADD COLUMN exception_sqrt  BOOLEAN          NOT NULL DEFAULT FALSE;   -- -hpl > var_h_sqrt
UPDATE backtest_results SET exception_raw = exception;
COMMENT ON COLUMN backtest_results.exception IS 'Official: -hpl > horizon-consistent VaR (block bootstrap when h >= 2). exception_raw and exception_sqrt are the cross-checks.';

ALTER TABLE backtest_summaries
    ADD COLUMN exceptions_raw  INTEGER,
    ADD COLUMN exceptions_sqrt INTEGER;
