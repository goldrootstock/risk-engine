# risk-engine

A portfolio market-risk engine for a multi-asset book — U.S. Treasury yields, G10/Asia FX and
energy spot — built in Python 3.12 on PostgreSQL 17. It loads public data (Federal Reserve
H.15 via FRED, ECB reference rates, EIA spot prices), builds a factor change matrix, computes
1-day **Expected Shortfall 97.5 % and VaR 99 %** by filtered historical simulation (EWMA/GARCH
scaling, date-wise joint residual sampling) with parametric and Monte Carlo cross-checks, and
validates the result the way a regulator would: Kupiec, Christoffersen, Basel traffic light, a
PLA-style test, and a stress module. On top of it sits a **CCP-style margin module**: a 2-day
MPOR initial margin (FHS ES 99 %, EMIR-style volatility floor and stress blend, liquidity and
concentration add-ons), a legacy SPAN 16-scenario comparison, a CPMI-IOSCO coverage backtest
and a Cover-2 default fund over four clearing members. Every parameter is pinned by a test and
every run records the hash of the configuration it used.

**Start here: [`docs/model_document.md`](docs/model_document.md)** — the SR 11-7-style model
document. It is written as an argument, not a results list: *every backtest passed — so is the
model trustworthy?*

## What the validation found

- **6,179 daily backtests (2001–2026) on a 29-series sample.** Official exceptions 50 vs 61.8
  expected at 99 %; all 25 Basel windows green; Christoffersen rejects one window. (Raw 1-day
  VaR: 63 exceptions, two yellow windows — see below why raw is the wrong comparison.)
- **The worst exception was 6.39× VaR** (2020-04-20, WTI −55.29 USD/bbl). The volatility filter
  was exactly one day late, and no frequency test can see the size of that day.
- **A horizon mismatch appeared only after changing the aggregation unit.** By calendar day the
  excess after gaps is not significant (p = 0.10); by business day, transitions spanning
  ≥ 2 business days had 15 exceptions in 340 (4.41 %, p = 2.5×10⁻⁶). Weekends were diluting
  the signal. Fixed with an h-day block bootstrap of residuals; raw and √h are kept alongside.
- **Breaking correlations does not diversify this book, it hurts it: independent-sample ES is
  1.74× the joint ES**, because the WTI-long / Brent-short hedge depends on the two crudes
  moving together. The 2020 negative-WTI day is the real-world instance.
- **Cross-checks agree.** Parametric ≈ Monte Carlo on the linear book (0.4 %); parametric
  ES/VaR = 1.005 (normal theory 1.005) vs FHS 1.038; the stress replay of 2020-04-20 reproduces
  the backtest's worst loss to the dollar (10,790,530); FRED and Treasury yields matched on
  every overlapping date.
- **The margin covers 99.92 % of 6,178 two-day transitions (5 breaches, 61.8 expected at
  99 %) — a pass that is also over-margining:** 21 of 25 windows have no breach at all and
  reject a two-sided Kupiec test on the conservative side. The core ES alone (no floor, no
  stress blend) already covers 99.50 %. The one day nothing covers is again 2020-04-20: loss
  3.34× the margin, 3.3× under legacy SPAN too.
- **Legacy SPAN asks for 1.9× the FHS margin and still breaches on the two tail days.**
  Summing per-instrument scan ranges with partial spread credits is expensive in normal
  markets and weak in the tail.
- **The default fund is set by the hedge book — the one that looks safest by frequency.**
  Over the 2-day close-out horizon a CCP actually faces, the official Cover-2 (24.3 M) is set
  by the WTI-long/Brent-short member on 2020-04-17→20 at 3.4× its margin — the same
  correlation-dependent structure that raised the A1 ES 1.74×. That member has the fewest
  breaches of the five books (6) and the largest worst breach (5.43×): the A1 lesson that
  frequency tests do not see size, repeated on different data. The two larger figures the
  official number excludes — 28.8 M with Basel IRRBB supervisory shocks added, 49.6 M over
  whole scenario paths — are kept in the same table, because choosing the smaller number
  has to stay visible.
- **Legacy SPAN is inconsistent across books, not just expensive:** breaches range from 2 to
  27 on the same methodology (13×). It over-charges hedged books (2× the FHS margin) and
  under-charges the long-only rates book in volatility regime shifts (0.8×, 0.63× in 2022)
  because its scan ranges are unfiltered trailing quantiles and its only diversification is
  an ad-hoc credit table — the reason CCPs moved to VaR-based margin.

## Reproduce

```bash
python3.12 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev,app]"
cp .env.example .env            # add FRED_API_KEY and EIA_API_KEY (free)
docker compose up -d && python -m risk_engine.data.migrate
python -m risk_engine.data.etl sync                                   # 31 series, full history
python -m risk_engine.risk load-positions config/positions_main.csv
python -m risk_engine.risk backfill --from 1999-01-05 --to 2026-09-09 --universe from_1999 --tag daily_batch
python -m risk_engine.backtest run --universe from_1999 --portfolio MAIN
python -m risk_engine.risk stress --as-of 2026-09-09 --universe from_1999 --portfolio MAIN
python -m risk_engine.margin load-members                             # four clearing members
python -m risk_engine.margin backfill --portfolio MAIN --universe from_1999 --tag margin_batch --from 2001-05-15 --to 2026-09-09
python -m risk_engine.margin coverage --portfolio MAIN --universe from_1999
python -m risk_engine.margin default-fund --as-of 2026-09-09 --universe from_1999 --run-margin
make check                                                            # ruff, mypy, pytest (needs the DB)
make api                                                              # read-only FastAPI on :8000 (/docs)
make dashboard                                                        # Streamlit on :8501
```

Raw vendor data is never committed; the ETL re-downloads it from the original sources.

## How it was built

Design and validation judgement are mine; the implementation was done in a pair with Claude
Code under an explicit contract ([`CLAUDE.md`](CLAUDE.md) §5). Concretely:

- I set the design: the schema and its rationale, the sample sets, the return definitions
  (bp for yields, absolute USD for energy because WTI went negative), the choice of block
  bootstrap over √h for multi-day horizons, the stress windows, and the rule that checks and
  validations are read-only and that parameters are never changed after seeing a result.
- The assistant wrote the code, the tests and the first drafts of the design notes; I reviewed
  and amended each note before the corresponding code went in (early modules were written
  signatures-and-tests first, bodies by me; the commit history shows where that boundary
  moved on 2026-09-13).
- The questions that produced the findings above — *is 19/63 after gaps normal? define gap;
  give me the baseline* — were mine; the measurement and the honest write-up were the
  assistant's job. The design notes in `docs/design/` keep that dialogue.

## Limits

Frequency tests pass, but the tail *size* is outside them (6.39×), the multi-day horizon needed
a correction that leaves the model conservative (0.81 % exceptions, four zero-exception windows
reject Kupiec on the low side), the PLA test has no power on a linear book, DGS30 for
2002–2006 is an H.15 estimate rather than an observation, and the portfolio is an arbitrary
book in which energy carries 82 % of ES. The margin module prices the spot book as if it were
cleared with zero carry — which removes the very mechanism (storage cost and contango) that
made 2020-04-20 happen — and its coverage test is self-consistent by construction. Full
discussion: model document §7.

## Layout

```
config/          universe, sample sets, risk / backtest / stress / margin parameters, members (all pinned by tests)
db/migrations/   plain-SQL schema, one transaction each
src/risk_engine/ data (ETL), risk (returns, FHS, parametric, MC, stress), backtest, margin (IM, SPAN, coverage, default fund), app (read-only API + dashboard)
docs/design/     numbered design notes — proposals, counter-proposals, decisions
docs/            decisions.md (every parameter with its source), walkthrough.md, model_document.md
```
