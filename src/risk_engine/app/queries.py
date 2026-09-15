"""Read-only queries shared by the API and the dashboard (design note 08 §2).

One SQL per question, called from both surfaces so their numbers cannot diverge. Every
function takes an open connection and returns JSON-ready values (ISO date strings, floats,
dicts, lists); pandas frames are built by the dashboard only.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import psycopg

# A batch of backtest_summaries rows is one record.write() transaction; PostgreSQL's now()
# is the transaction start time, so all rows of a batch share created_at exactly.
LATEST_BATCH_SQL = """
SELECT summary_id, window_start, window_end, n_obs, exceptions, expected, exceptions_raw,
       exceptions_sqrt, kupiec_lr, kupiec_p, christoffersen_lr, christoffersen_p, cc_lr, cc_p,
       traffic_light, pla_spearman, pla_ks, pla_zone, params, code_version, created_at
FROM backtest_summaries
WHERE universe = %(universe)s AND portfolio_code = %(portfolio)s
  AND created_at = (SELECT max(created_at) FROM backtest_summaries
                    WHERE universe = %(universe)s AND portfolio_code = %(portfolio)s)
ORDER BY window_start
"""

TOTALS_SQL = """
SELECT count(*), count(*) FILTER (WHERE exception), count(*) FILTER (WHERE exception_raw),
       count(*) FILTER (WHERE exception_sqrt), count(*) FILTER (WHERE h_business_days >= 2),
       count(*) FILTER (WHERE h_business_days >= 2 AND exception),
       count(*) FILTER (WHERE h_business_days >= 2 AND exception_raw),
       min(as_of_date), max(as_of_date)
FROM backtest_results
WHERE universe = %(universe)s AND portfolio_code = %(portfolio)s
"""

DAYS_SQL = """
SELECT run_id, as_of_date, pnl_date, h_business_days, hpl, rtpl, var_99, es_975, var_h_block,
       var_h_sqrt, exception, exception_raw, exception_sqrt, attribution
FROM backtest_results
WHERE universe = %(universe)s AND portfolio_code = %(portfolio)s
  AND (%(start)s::date IS NULL OR as_of_date >= %(start)s)
  AND (%(end)s::date IS NULL OR as_of_date <= %(end)s)
  AND (NOT %(only)s OR exception OR exception_raw)
ORDER BY as_of_date
"""

SERIES_SQL = """
SELECT r.run_id, r.as_of_date, r.portfolio_value,
       max(m.value) FILTER (WHERE m.measure = 'var' AND m.confidence = 0.99)  AS var_99,
       max(m.value) FILTER (WHERE m.measure = 'es'  AND m.confidence = 0.975) AS es_975,
       max(m.value) FILTER (WHERE m.measure = 'stressed_es')                  AS stressed_es
FROM risk_runs r JOIN risk_measures m USING (run_id)
WHERE r.universe = %(universe)s AND r.portfolio_code = %(portfolio)s AND r.tag = %(tag)s
  AND r.method = %(method)s AND r.horizon_days = %(horizon_days)s AND m.scope_type = 'portfolio'
  AND (%(start)s::date IS NULL OR r.as_of_date >= %(start)s)
  AND (%(end)s::date IS NULL OR r.as_of_date <= %(end)s)
GROUP BY r.run_id ORDER BY r.as_of_date
"""

LATEST_RUN_SQL = """
SELECT run_id, as_of_date, positions_as_of, method, tag, horizon_days, window_days, n_scenarios,
       portfolio_value, base_currency, params, code_version
FROM risk_runs
WHERE universe = %(universe)s AND portfolio_code = %(portfolio)s AND method = %(method)s
  AND horizon_days = %(horizon_days)s
  AND (%(tag)s::text IS NULL OR tag = %(tag)s)
  AND (%(as_of)s::date IS NULL OR as_of_date <= %(as_of)s)
ORDER BY as_of_date DESC, run_id DESC
LIMIT 1
"""

MEASURES_SQL = """
SELECT measure, confidence, scope_type, scope_key, value
FROM risk_measures WHERE run_id = %(run_id)s
ORDER BY scope_type, measure, scope_key
"""

CATALOG_SQL = """
SELECT universe, portfolio_code, tag, method, horizon_days, count(*), min(as_of_date),
       max(as_of_date)
FROM risk_runs GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, 2, 3, 4, 5
"""

STRESS_RUN_SQL = """
SELECT stress_run_id, as_of_date, positions_as_of, portfolio_value, es_975,
       scenario_set_sha256, params, code_version, created_at
FROM stress_runs
WHERE universe = %(universe)s AND portfolio_code = %(portfolio)s
  AND (%(as_of)s::date IS NULL OR as_of_date <= %(as_of)s)
ORDER BY as_of_date DESC, stress_run_id DESC
LIMIT 1
"""

STRESS_RESULTS_SQL = """
SELECT scenario, kind, window_start, window_end, loss, worst_day, worst_day_loss,
       loss_over_es, attribution
FROM stress_results WHERE stress_run_id = %(id)s
ORDER BY kind, loss DESC
"""

Row = dict[str, Any]


def _iso(d: Any) -> Any:
    return d.isoformat() if isinstance(d, date) else d


def _num(v: Any) -> Any:
    return float(v) if v is not None and not isinstance(v, bool | str | dict) else v


def catalog(conn: psycopg.Connection[Any]) -> list[Row]:
    """Every (universe, portfolio, tag, method, horizon) series present, with counts and dates."""
    return [
        {
            "universe": r[0],
            "portfolio": r[1],
            "tag": r[2],
            "method": r[3],
            "horizon_days": int(r[4]),
            "n_runs": int(r[5]),
            "first": _iso(r[6]),
            "last": _iso(r[7]),
        }
        for r in conn.execute(CATALOG_SQL).fetchall()
    ]


def headline_series(
    conn: psycopg.Connection[Any],
    universe: str,
    portfolio: str,
    *,
    tag: str = "daily_batch",
    method: str = "fhs",
    horizon_days: int = 1,
    start: date | None = None,
    end: date | None = None,
) -> list[Row]:
    """Portfolio VaR 99 / ES 97.5 / stressed ES per run date (losses positive).

    Selects the series positively by ``(tag, method, horizon_days)``: the 2-day margin
    series shares ``risk_runs`` (note 01 §10-2) and must never be mixed into the 1-day
    risk series. ``tests/test_app_api.py::test_readers_ignore_margin_shaped_runs`` guards this.
    """
    rows = conn.execute(
        SERIES_SQL,
        {
            "universe": universe,
            "portfolio": portfolio,
            "tag": tag,
            "method": method,
            "horizon_days": horizon_days,
            "start": start,
            "end": end,
        },
    ).fetchall()
    return [
        {
            "run_id": int(r[0]),
            "as_of": _iso(r[1]),
            "portfolio_value": _num(r[2]),
            "var_99": _num(r[3]),
            "es_975": _num(r[4]),
            "stressed_es": _num(r[5]),
        }
        for r in rows
    ]


def latest_run(
    conn: psycopg.Connection[Any],
    universe: str,
    portfolio: str,
    *,
    method: str = "fhs",
    tag: str | None = None,
    horizon_days: int = 1,
    as_of: date | None = None,
) -> Row | None:
    """Header, portfolio measures and instrument component ES of the newest run on/before ``as_of``.

    Returns ``None`` when no run matches. ``tag=None`` accepts any tag (the most recent
    ad-hoc run wins over an older batch run on the same date only by ``run_id``).
    ``horizon_days`` is always selected positively (default 1): margin runs at the 2-day
    MPOR live in the same table and must not be returned as risk runs (note 01 §10-2).
    """
    r = conn.execute(
        LATEST_RUN_SQL,
        {
            "universe": universe,
            "portfolio": portfolio,
            "method": method,
            "tag": tag,
            "horizon_days": horizon_days,
            "as_of": as_of,
        },
    ).fetchone()
    if r is None:
        return None
    run_id = int(r[0])
    portfolio_measures: list[Row] = []
    component_es: dict[str, float] = {}
    by_scope: dict[str, list[Row]] = {}
    for m in conn.execute(MEASURES_SQL, {"run_id": run_id}).fetchall():
        item = {
            "measure": m[0],
            "confidence": _num(m[1]),
            "scope_type": m[2],
            "scope_key": m[3],
            "value": float(m[4]),
        }
        if m[2] == "portfolio":
            portfolio_measures.append(item)
        elif m[2] == "instrument" and m[0] == "component_es":
            component_es[m[3]] = float(m[4])
        else:
            by_scope.setdefault(m[2], []).append(item)
    return {
        "run_id": run_id,
        "universe": universe,
        "portfolio": portfolio,
        "as_of": _iso(r[1]),
        "positions_as_of": _iso(r[2]),
        "method": r[3],
        "tag": r[4],
        "horizon_days": int(r[5]),
        "window_days": int(r[6]),
        "n_scenarios": int(r[7]),
        "portfolio_value": float(r[8]),
        "base_currency": r[9],
        "params": r[10],
        "code_version": r[11],
        "measures": portfolio_measures,
        "component_es": component_es,
        "other_scopes": by_scope,
    }


def portfolio_measure(run: Row, measure: str, confidence: float | None) -> Row | None:
    """Pick one portfolio-level measure out of :func:`latest_run`'s result."""
    measures: list[Row] = run["measures"]
    for m in measures:
        if m["measure"] == measure and (confidence is None or m["confidence"] == confidence):
            return m
    return None


def backtest_latest(conn: psycopg.Connection[Any], universe: str, portfolio: str) -> Row | None:
    """Window table of the newest backtest batch plus day-level totals."""
    key = {"universe": universe, "portfolio": portfolio}
    win = conn.execute(LATEST_BATCH_SQL, key).fetchall()
    if not win:
        return None
    t = conn.execute(TOTALS_SQL, key).fetchone()
    assert t is not None
    params = win[0][18] or {}
    return {
        "universe": universe,
        "portfolio": portfolio,
        "horizon_method": params.get("horizon_method"),
        "params": params,
        "code_version": win[0][19],
        "created_at": win[0][20].isoformat(),
        "totals": {
            "days": int(t[0]),
            "exceptions": int(t[1]),
            "exceptions_raw": int(t[2]),
            "exceptions_sqrt": int(t[3]),
            "expected": float(t[0]) * (1.0 - float(params.get("confidence", 0.99))),
            "multi_day_transitions": int(t[4]),
            "multi_day_exceptions": int(t[5]),
            "multi_day_exceptions_raw": int(t[6]),
            "first": _iso(t[7]),
            "last": _iso(t[8]),
        },
        "windows": [
            {
                "summary_id": int(w[0]),
                "window_start": _iso(w[1]),
                "window_end": _iso(w[2]),
                "n_obs": int(w[3]),
                "exceptions": int(w[4]),
                "expected": float(w[5]),
                "exceptions_raw": _num(w[6]),
                "exceptions_sqrt": _num(w[7]),
                "kupiec_lr": _num(w[8]),
                "kupiec_p": _num(w[9]),
                "christoffersen_lr": _num(w[10]),
                "christoffersen_p": _num(w[11]),
                "cc_lr": _num(w[12]),
                "cc_p": _num(w[13]),
                "traffic_light": w[14],
                "pla_spearman": _num(w[15]),
                "pla_ks": _num(w[16]),
                "pla_zone": w[17],
            }
            for w in win
        ],
    }


def backtest_days(
    conn: psycopg.Connection[Any],
    universe: str,
    portfolio: str,
    *,
    start: date | None = None,
    end: date | None = None,
    exceptions_only: bool = False,
) -> list[Row]:
    """Day rows: P&L, 1-day and h-day VaR, official / raw / sqrt exception flags, attribution."""
    rows = conn.execute(
        DAYS_SQL,
        {
            "universe": universe,
            "portfolio": portfolio,
            "start": start,
            "end": end,
            "only": exceptions_only,
        },
    ).fetchall()
    return [
        {
            "run_id": int(r[0]),
            "as_of": _iso(r[1]),
            "pnl_date": _iso(r[2]),
            "h": int(r[3]),
            "hpl": float(r[4]),
            "rtpl": float(r[5]),
            "var_99": float(r[6]),
            "es_975": float(r[7]),
            "var_h_block": _num(r[8]),
            "var_h_sqrt": _num(r[9]),
            "exception": bool(r[10]),
            "exception_raw": bool(r[11]),
            "exception_sqrt": bool(r[12]),
            "attribution": r[13],
        }
        for r in rows
    ]


def stress_latest(
    conn: psycopg.Connection[Any], universe: str, portfolio: str, *, as_of: date | None = None
) -> Row | None:
    """Newest stress run on/before ``as_of`` with all scenario results."""
    r = conn.execute(
        STRESS_RUN_SQL, {"universe": universe, "portfolio": portfolio, "as_of": as_of}
    ).fetchone()
    if r is None:
        return None
    res = conn.execute(STRESS_RESULTS_SQL, {"id": r[0]}).fetchall()
    return {
        "stress_run_id": int(r[0]),
        "universe": universe,
        "portfolio": portfolio,
        "as_of": _iso(r[1]),
        "positions_as_of": _iso(r[2]),
        "portfolio_value": float(r[3]),
        "es_975": float(r[4]),
        "scenario_set_sha256": r[5],
        "params": r[6],
        "code_version": r[7],
        "created_at": r[8].isoformat(),
        "results": [
            {
                "scenario": s[0],
                "kind": s[1],
                "window_start": _iso(s[2]),
                "window_end": _iso(s[3]),
                "loss": float(s[4]),
                "worst_day": _iso(s[5]),
                "worst_day_loss": _num(s[6]),
                "loss_over_es": _num(s[7]),
                "attribution": s[8],
            }
            for s in res
        ],
    }
