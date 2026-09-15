"""Read-only FastAPI surface over recorded results (design note 08 §3).

``uvicorn risk_engine.app.api:app``. Every route is GET; every connection is opened with
``default_transaction_read_only = on`` so that no code path — present or future — can write
through the API (note 00 §3).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Annotated, Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query
from psycopg.conninfo import conninfo_to_dict

from risk_engine.app import queries
from risk_engine.settings import load_settings

READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"

app = FastAPI(
    title="risk-engine",
    version="0.1.0",
    description="Recorded ES / VaR, backtest, stress and margin results. Read-only; writes "
    "happen through the CLI only.",
)


def connect_read_only(database_url: str) -> psycopg.Connection[Any]:
    """Open an autocommit connection that PostgreSQL itself refuses to let write.

    ``options`` already present in the URL (e.g. a ``search_path``) are kept: a keyword
    ``options=`` would silently replace them, which is how the dashboard smoke test once
    read the shared database instead of its throwaway schema.
    """
    existing = conninfo_to_dict(database_url).get("options", "")
    merged = f"{existing} {READ_ONLY_OPTIONS}".strip()
    return psycopg.connect(database_url, autocommit=True, options=merged)


def get_conn() -> Iterator[psycopg.Connection[Any]]:
    """Per-request connection (overridden in tests with a throwaway-schema connection)."""
    url = getattr(app.state, "database_url", None) or load_settings().database_url
    with connect_read_only(url) as conn:
        yield conn


Conn = Annotated[psycopg.Connection[Any], Depends(get_conn)]
Universe = Annotated[str, Query(description="sample set, e.g. from_1999 or default")]
Portfolio = Annotated[str, Query(description="portfolio code, e.g. MAIN")]
AsOf = Annotated[date | None, Query(description="newest run on or before this date")]


@app.get("/health")
def health(conn: Conn) -> dict[str, Any]:
    """Liveness plus a round trip to the database."""
    row = conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "db": row == (1,)}


@app.get("/catalog")
def catalog(conn: Conn) -> list[dict[str, Any]]:
    """Which (universe, portfolio, tag, method, horizon_days) series exist."""
    return queries.catalog(conn)


def _run_or_404(
    conn: psycopg.Connection[Any],
    universe: str,
    portfolio: str,
    method: str,
    tag: str | None,
    as_of: date | None,
) -> dict[str, Any]:
    run = queries.latest_run(conn, universe, portfolio, method=method, tag=tag, as_of=as_of)
    if run is None:
        raise HTTPException(404, f"no {method} run for {universe}/{portfolio} on or before {as_of}")
    return run


def _headline(run: dict[str, Any], measure: str, confidence: float) -> dict[str, Any]:
    m = queries.portfolio_measure(run, measure, confidence)
    if m is None:
        raise HTTPException(404, f"run {run['run_id']} has no {measure}@{confidence}")
    return {
        "measure": measure,
        "confidence": confidence,
        "value": m["value"],
        "unit": run["base_currency"],
        "fraction_of_portfolio_value": m["value"] / run["portfolio_value"],
        "run_id": run["run_id"],
        "as_of": run["as_of"],
        "method": run["method"],
        "universe": run["universe"],
        "portfolio": run["portfolio"],
        "horizon_days": run["horizon_days"],
        "risk_params_sha256": (run["params"] or {}).get("risk_params_sha256"),
        "code_version": run["code_version"],
    }


@app.get("/es")
def es(
    conn: Conn,
    universe: Universe = "from_1999",
    portfolio: Portfolio = "MAIN",
    method: str = "fhs",
    tag: str | None = None,
    as_of: AsOf = None,
) -> dict[str, Any]:
    """Expected Shortfall 97.5 % of the newest matching run (loss positive)."""
    return _headline(_run_or_404(conn, universe, portfolio, method, tag, as_of), "es", 0.975)


@app.get("/var")
def var(
    conn: Conn,
    universe: Universe = "from_1999",
    portfolio: Portfolio = "MAIN",
    method: str = "fhs",
    tag: str | None = None,
    as_of: AsOf = None,
) -> dict[str, Any]:
    """VaR 99 % of the newest matching run (loss positive)."""
    return _headline(_run_or_404(conn, universe, portfolio, method, tag, as_of), "var", 0.99)


@app.get("/runs/latest")
def runs_latest(
    conn: Conn,
    universe: Universe = "from_1999",
    portfolio: Portfolio = "MAIN",
    method: str = "fhs",
    tag: str | None = None,
    as_of: AsOf = None,
) -> dict[str, Any]:
    """Full header, all portfolio measures and instrument component ES of the newest run."""
    return _run_or_404(conn, universe, portfolio, method, tag, as_of)


@app.get("/series")
def series(
    conn: Conn,
    universe: Universe = "from_1999",
    portfolio: Portfolio = "MAIN",
    tag: str = "daily_batch",
    method: str = "fhs",
    start: date | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    """VaR 99 / ES 97.5 / stressed ES per run date."""
    return queries.headline_series(
        conn, universe, portfolio, tag=tag, method=method, start=start, end=end
    )


@app.get("/backtest")
def backtest(
    conn: Conn, universe: Universe = "from_1999", portfolio: Portfolio = "MAIN"
) -> dict[str, Any]:
    """Newest backtest batch: window statistics and official / raw / sqrt totals."""
    bt = queries.backtest_latest(conn, universe, portfolio)
    if bt is None:
        raise HTTPException(404, f"no backtest for {universe}/{portfolio}")
    return bt


@app.get("/backtest/days")
def backtest_days(
    conn: Conn,
    universe: Universe = "from_1999",
    portfolio: Portfolio = "MAIN",
    start: date | None = None,
    end: date | None = None,
    exceptions_only: bool = False,
) -> list[dict[str, Any]]:
    """Day rows of the backtest (P&L, VaR, h, exception flags, attribution)."""
    return queries.backtest_days(
        conn, universe, portfolio, start=start, end=end, exceptions_only=exceptions_only
    )


@app.get("/stress")
def stress(
    conn: Conn,
    universe: Universe = "from_1999",
    portfolio: Portfolio = "MAIN",
    as_of: AsOf = None,
) -> dict[str, Any]:
    """Newest stress run on or before ``as_of`` with every scenario's loss."""
    st = queries.stress_latest(conn, universe, portfolio, as_of=as_of)
    if st is None:
        raise HTTPException(404, f"no stress run for {universe}/{portfolio}")
    return st


@app.get("/margin")
def margin(
    conn: Conn,
    universe: Universe = "from_1999",
    portfolio: Portfolio = "MAIN",
    tag: str = "margin_batch",
    horizon_days: int = 2,
    as_of: AsOf = None,
) -> dict[str, Any]:
    """Newest margin run: IM decomposition, MPOR tail measures, per-instrument add-ons."""
    run = queries.margin_latest(
        conn, universe, portfolio, tag=tag, horizon_days=horizon_days, as_of=as_of
    )
    if run is None:
        raise HTTPException(404, f"no margin run for {universe}/{portfolio} on or before {as_of}")
    return run


@app.get("/margin/coverage")
def margin_coverage(
    conn: Conn, universe: Universe = "from_1999", portfolio: Portfolio = "MAIN"
) -> dict[str, Any]:
    """Newest margin coverage batch: window statistics and day-level totals."""
    cv = queries.coverage_latest(conn, universe, portfolio)
    if cv is None:
        raise HTTPException(404, f"no coverage backtest for {universe}/{portfolio}")
    return cv


@app.get("/margin/coverage/days")
def margin_coverage_days(
    conn: Conn,
    universe: Universe = "from_1999",
    portfolio: Portfolio = "MAIN",
    start: date | None = None,
    end: date | None = None,
    breaches_only: bool = False,
) -> list[dict[str, Any]]:
    """Day rows of the coverage backtest (realised loss, IM yardsticks, breach flags)."""
    return queries.coverage_days(
        conn, universe, portfolio, start=start, end=end, breaches_only=breaches_only
    )


@app.get("/default-fund")
def default_fund(
    conn: Conn, universe: Universe = "from_1999", basis: str = "mpor", as_of: AsOf = None
) -> dict[str, Any]:
    """Newest Cover-N default fund sizing (basis mpor = official, path = path analysis)."""
    if basis not in ("mpor", "path"):
        raise HTTPException(422, "basis must be 'mpor' or 'path'")
    dfr = queries.default_fund_latest(conn, universe, basis=basis, as_of=as_of)
    if dfr is None:
        raise HTTPException(404, f"no default fund run for {universe} on basis {basis}")
    return dfr
