# risk-engine

Portfolio Risk & CCP Margin Engine — an independent project in Python / PostgreSQL.

- **Risk module**: filtered historical simulation (EWMA / GARCH volatility scaling), Expected Shortfall 97.5% (FRTB-style, stressed window) and VaR 99%, component / incremental attribution, historical and hypothetical stress scenarios.
- **Validation**: regulatory VaR/ES backtesting (Kupiec POF, Christoffersen independence, Basel traffic light) and a PLA-style Spearman / KS test; SR 11-7-style model documentation.
- **Margin module** (later): VaR/ES-based initial margin in the style of SPAN 2 / IRM 2 (2-day MPOR, anti-procyclicality floors, liquidity add-on, spot/futures cross-margining), legacy SPAN comparison, margin coverage backtest, Cover-2 default-fund sizing.

Status: week 1 — data layer. Design notes live in [`docs/design/`](docs/design/).

## Requirements

- Python 3.12 (see `.python-version`)
- Docker (for the local PostgreSQL)

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
docker compose up -d                    # PostgreSQL 17 on localhost:5432
python -m risk_engine.data.migrate      # apply db/migrations/*.sql
python -m risk_engine.data.migrate status
```

Migrations are plain SQL files in `db/migrations/NNNN_name.sql`, applied in order and recorded in `schema_migrations`. Each file runs in its own transaction.

## Develop

```bash
ruff check . && ruff format .   # lint + format
mypy                            # type check (src/)
pytest                          # tests; `db`-marked tests need DATABASE_URL
pytest --cov                    # with coverage
```

`pytest` skips tests marked `db` unless `DATABASE_URL` is set. Export it (or `set -a; source .env; set +a`) to run them locally; CI always runs them against a service container.

## Conventions

**Sign of losses.** VaR, ES, margin and realised losses are stored and reported as **positive numbers** in the base currency (USD). The sign is flipped in exactly two places: once when scenario P&L is turned into a loss distribution (`loss = -pnl`), and once when a VaR/ES band is overlaid on a P&L chart. Everywhere else, including the database and API responses, a larger number means a larger loss. Rationale and the full table are in `docs/design/01-data-layer-schema.md` §1-1.

**Numeric types.** Measured values (prices, risk numbers) are `DOUBLE PRECISION` and arrive in Python as `float`; ledger values (quantities, multipliers) are `NUMERIC` and arrive as `Decimal`.

## Layout

```
db/migrations/  plain-SQL schema migrations
src/risk_engine/
  settings.py DATABASE_URL / MIGRATIONS_DIR from env or .env
  data/       migration runner, ETL
  risk/       return matrix, FHS, ES / VaR
  backtest/   Kupiec, Christoffersen, traffic light, PLA
tests/
docs/design/  numbered design notes (approved before code)
```
