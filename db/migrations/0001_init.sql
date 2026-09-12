-- 0001_init: instrument master, daily prices, position snapshots.
-- Design note: docs/design/01-data-layer-schema.md (decisions 1, 2, 5 approved 2026-09-12).
-- risk_runs / risk_measures follow in 0002 once decision 3 is approved.

CREATE TABLE instruments (
    instrument_id   BIGINT        GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source          TEXT          NOT NULL,
    ticker          TEXT          NOT NULL,
    name            TEXT          NOT NULL,
    asset_class     TEXT          NOT NULL
                                  CHECK (asset_class IN ('equity', 'rates', 'fx', 'commodity')),
    instrument_type TEXT          NOT NULL
                                  CHECK (instrument_type IN ('stock', 'etf', 'future', 'fx_spot', 'index')),
    quote_type      TEXT          NOT NULL DEFAULT 'price'
                                  CHECK (quote_type IN ('price', 'yield')),
    currency        CHAR(3)       NOT NULL,
    multiplier      NUMERIC(18,6) NOT NULL DEFAULT 1 CHECK (multiplier > 0),
    is_active       BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    UNIQUE (source, ticker)
);

COMMENT ON TABLE  instruments                 IS 'Instrument master. Never deleted; retire with is_active = false.';
COMMENT ON COLUMN instruments.source          IS 'Data vendor the ticker belongs to (yfinance, fred, ...). Same ticker on two sources = two rows.';
COMMENT ON COLUMN instruments.asset_class     IS 'Factor bucket for risk contribution: equity | rates | fx | commodity.';
COMMENT ON COLUMN instruments.instrument_type IS 'Needed by the margin module (spot vs futures cross-margining, futures roll).';
COMMENT ON COLUMN instruments.quote_type      IS 'price -> log returns on adj_close; yield -> first differences in bp.';
COMMENT ON COLUMN instruments.currency        IS 'Quote currency (ISO 4217). FX rates for conversion are themselves instruments (asset_class = fx).';
COMMENT ON COLUMN instruments.multiplier      IS 'Contract multiplier. P&L = quantity * multiplier * price change.';

CREATE TABLE prices (
    instrument_id BIGINT           NOT NULL REFERENCES instruments (instrument_id),
    price_date    DATE             NOT NULL,
    close         DOUBLE PRECISION NOT NULL,
    adj_close     DOUBLE PRECISION NOT NULL,
    volume        BIGINT,
    loaded_at     TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, price_date)
);

CREATE INDEX prices_price_date_idx ON prices (price_date);

COMMENT ON TABLE  prices           IS 'Daily closes exactly as received from the vendor: no calendar alignment, no fill. Upsert on (instrument_id, price_date).';
COMMENT ON COLUMN prices.close     IS 'Unadjusted close: used to value positions.';
COMMENT ON COLUMN prices.adj_close IS 'Dividend/split-adjusted close: used for returns.';
COMMENT ON COLUMN prices.volume    IS 'NULL where the instrument has no volume (FX, indices). Used for liquidity add-ons (ADV).';
COMMENT ON COLUMN prices.loaded_at IS 'When this value was last written; vendors restate history.';

CREATE TABLE positions (
    portfolio_code TEXT          NOT NULL,
    as_of_date     DATE          NOT NULL,
    instrument_id  BIGINT        NOT NULL REFERENCES instruments (instrument_id),
    quantity       NUMERIC(20,6) NOT NULL CHECK (quantity <> 0),
    loaded_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (portfolio_code, as_of_date, instrument_id)
);

COMMENT ON TABLE  positions            IS 'Holdings snapshot at the close of as_of_date. A run on date D uses the latest snapshot with as_of_date <= D.';
COMMENT ON COLUMN positions.quantity   IS 'Signed: negative = short. Zero is not a position; close-outs delete the row.';
