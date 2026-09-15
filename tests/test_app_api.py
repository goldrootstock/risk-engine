"""Read-only API and dashboard over a throwaway schema seeded by hand (design note 08 §5)."""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import psycopg
import pytest
from psycopg.types.json import Jsonb

from risk_engine.app import queries
from risk_engine.app.api import app, connect_read_only, get_conn
from risk_engine.data.migrate import upgrade

from .conftest import MIGRATIONS_DIR, REPO_ROOT

fastapi_testclient = pytest.importorskip("fastapi.testclient")

pytestmark = pytest.mark.db


def _seed(conn: psycopg.Connection[Any]) -> None:
    """Two fhs runs, one backtest batch (twice, to test 'latest'), one stress run.

    Plus one *margin-shaped* run (2-day MPOR, tag margin_batch, newest as_of and run_id)
    that every 1-day reader must ignore (note 01 §10-2, JK approval 2026-09-15).
    """
    ids = []
    for as_of, var, es in ((date(2024, 1, 10), 800.0, 900.0), (date(2024, 1, 11), 820.0, 950.0)):
        got = conn.execute(
            """INSERT INTO risk_runs (portfolio_code, as_of_date, positions_as_of, method,
                   window_days, n_scenarios, portfolio_value, params, tag, universe)
               VALUES ('T', %s, '2024-01-01', 'fhs', 500, 500, 100000.0, %s, 'daily_batch', 'u')
               RETURNING run_id""",
            (as_of, Jsonb({"risk_params_sha256": "a" * 64})),
        ).fetchone()
        assert got is not None
        ids.append(int(got[0]))
        conn.execute(
            """INSERT INTO risk_measures (run_id, measure, confidence, scope_type, scope_key, value)
               VALUES (%(r)s, 'var', 0.99, 'portfolio', '', %(v)s),
                      (%(r)s, 'es', 0.975, 'portfolio', '', %(e)s),
                      (%(r)s, 'stressed_es', 0.975, 'portfolio', '', 1500.0),
                      (%(r)s, 'component_es', 0.975, 'instrument', 'WTI', %(c1)s),
                      (%(r)s, 'component_es', 0.975, 'instrument', 'EURUSD', %(c2)s)""",
            {"r": ids[-1], "v": var, "e": es, "c1": es * 0.7, "c2": es * 0.3},
        )
    conn.execute(
        """INSERT INTO backtest_results (run_id, universe, portfolio_code, as_of_date, pnl_date,
               hpl, rtpl, var_99, es_975, exception, attribution, h_business_days, var_h_block,
               var_h_sqrt, exception_raw, exception_sqrt)
           VALUES (%s, 'u', 'T', '2024-01-10', '2024-01-11', -500.0, -480.0, 800.0, 900.0, false,
                   '{}', 1, NULL, NULL, false, false),
                  (%s, 'u', 'T', '2024-01-11', '2024-01-15', -900.0, -850.0, 820.0, 950.0, false,
                   %s, 2, 1200.0, 1159.7, true, false)""",
        (ids[0], ids[1], Jsonb({"WTI": 700.0, "_total": 900.0})),
    )
    for batch in ("raw", "block"):
        conn.execute(
            """INSERT INTO backtest_summaries (universe, portfolio_code, window_start, window_end,
                   n_obs, exceptions, expected, kupiec_p, christoffersen_p, cc_p, traffic_light,
                   pla_spearman, pla_ks, pla_zone, params, exceptions_raw, exceptions_sqrt,
                   created_at)
               VALUES ('u', 'T', '2024-01-10', '2024-01-11', 2, 0, 0.02, 0.9, 1.0, 0.95, 'green',
                       0.99, 0.01, 'green', %s, 1, 0, now() + (%s || ' second')::interval)""",
            (Jsonb({"horizon_method": batch, "confidence": 0.99}), 1 if batch == "block" else 0),
        )
    margin = conn.execute(
        """INSERT INTO risk_runs (portfolio_code, as_of_date, positions_as_of, method,
               horizon_days, window_days, n_scenarios, portfolio_value, params, tag, universe)
           VALUES ('T', '2024-01-12', '2024-01-01', 'fhs', 2, 500, 499, 100000.0, '{}',
                   'margin_batch', 'u')
           RETURNING run_id"""
    ).fetchone()
    assert margin is not None
    conn.execute(
        """INSERT INTO risk_measures (run_id, measure, confidence, scope_type, scope_key, value)
           VALUES (%(r)s, 'var', 0.99, 'portfolio', '', 9999.0),
                  (%(r)s, 'es', 0.975, 'portfolio', '', 9999.0),
                  (%(r)s, 'im', NULL, 'portfolio', '', 12000.0),
                  (%(r)s, 'im_core', NULL, 'portfolio', '', 11000.0)""",
        {"r": int(margin[0])},
    )
    conn.execute(
        """INSERT INTO margin_coverage_results (run_id, universe, portfolio_code, as_of_date,
               pnl_date, h_business_days, realised_loss, im, im_core, im_span_legacy, im_h_block,
               breach, breach_raw, breach_core, breach_span, shortfall, attribution)
           VALUES (%s, 'u', 'T', '2024-01-12', '2024-01-16', 2, 13000.0, 12000.0, 11000.0,
                   20000.0, NULL, true, true, true, false, 1000.0, %s)""",
        (int(margin[0]), Jsonb({"WTI": 13000.0, "_total": 13000.0})),
    )
    conn.execute(
        """INSERT INTO margin_coverage_summaries (universe, portfolio_code, window_start,
               window_end, n_obs, breaches, breaches_raw, breaches_core, breaches_span, coverage,
               target, kupiec_p, max_shortfall, max_shortfall_over_im, params)
           VALUES ('u', 'T', '2024-01-12', '2024-01-12', 1, 1, 1, 1, 0, 0.0, 0.99, 0.001, 1000.0,
                   0.0833, %s)""",
        (Jsonb({"margin_params_sha256": "m" * 64}),),
    )
    dfr = conn.execute(
        """INSERT INTO default_fund_runs (universe, as_of_date, scenario_set_sha256,
               margin_params_sha256, n_members, cover, default_fund, binding_scenario,
               binding_members, params)
           VALUES ('u', '2024-01-12', %s, %s, 2, 2, 5000.0, 'gfc_2008', %s, '{}')
           RETURNING default_fund_run_id""",
        ("b" * 64, "m" * 64, ["T", "U"]),
    ).fetchone()
    assert dfr is not None
    conn.execute(
        """INSERT INTO default_fund_results (default_fund_run_id, scenario, kind, portfolio_code,
               im_run_id, stress_loss, im, uncovered)
           VALUES (%(d)s, 'gfc_2008', 'historical', 'T', %(r)s, 15000.0, 12000.0, 3000.0),
                  (%(d)s, 'gfc_2008', 'historical', 'U', %(r)s, 14000.0, 12000.0, 2000.0)""",
        {"d": int(dfr[0]), "r": int(margin[0])},
    )
    got = conn.execute(
        """INSERT INTO stress_runs (portfolio_code, universe, as_of_date, positions_as_of,
               portfolio_value, es_975, scenario_set_sha256, params)
           VALUES ('T', 'u', '2024-01-11', '2024-01-01', 100000.0, 950.0, 'b', '{}')
           RETURNING stress_run_id"""
    ).fetchone()
    assert got is not None
    conn.execute(
        """INSERT INTO stress_results (stress_run_id, scenario, kind, loss, loss_over_es,
               attribution)
           VALUES (%(s)s, 'gfc_2008', 'historical', 4750.0, 5.0, '{}'),
                  (%(s)s, 'independent', 'correlation', 1900.0, 2.0, '{}')""",
        {"s": int(got[0])},
    )


@pytest.fixture
def seeded(db_conn: psycopg.Connection[Any]) -> psycopg.Connection[Any]:
    upgrade(db_conn, MIGRATIONS_DIR)
    _seed(db_conn)
    return db_conn


@pytest.fixture
def client(seeded: psycopg.Connection[Any]) -> Any:
    app.dependency_overrides[get_conn] = lambda: seeded
    try:
        yield fastapi_testclient.TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_only_get_routes() -> None:
    methods = {m for r in app.routes for m in (getattr(r, "methods", None) or set())}
    assert methods <= {"GET", "HEAD"}, methods


def test_connection_is_read_only(seeded: psycopg.Connection[Any]) -> None:
    schema = seeded.execute("SHOW search_path").fetchone()
    assert schema is not None
    url = os.environ["DATABASE_URL"]
    with connect_read_only(url) as ro:
        ro.execute(f"SET search_path TO {schema[0]}")
        assert ro.execute("SELECT count(*) FROM risk_runs").fetchone() == (3,)
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            ro.execute("DELETE FROM risk_runs")


def test_headline_endpoints(client: Any) -> None:
    assert client.get("/health").json() == {"status": "ok", "db": True}
    es = client.get("/es", params={"universe": "u", "portfolio": "T"}).json()
    assert es["value"] == 950.0 and es["as_of"] == "2024-01-11" and es["confidence"] == 0.975
    assert es["fraction_of_portfolio_value"] == pytest.approx(0.0095)
    assert es["risk_params_sha256"] == "a" * 64
    var = client.get("/var", params={"universe": "u", "portfolio": "T", "as_of": "2024-01-10"})
    assert var.json()["value"] == 800.0 and var.json()["run_id"] < es["run_id"]
    assert client.get("/es", params={"universe": "nope"}).status_code == 404
    run = client.get("/runs/latest", params={"universe": "u", "portfolio": "T"}).json()
    assert run["component_es"] == {"WTI": 665.0, "EURUSD": 285.0}
    assert sum(run["component_es"].values()) == pytest.approx(950.0)
    cat = client.get("/catalog").json()
    assert cat == [
        {
            "universe": "u",
            "portfolio": "T",
            "tag": "daily_batch",
            "method": "fhs",
            "horizon_days": 1,
            "n_runs": 2,
            "first": "2024-01-10",
            "last": "2024-01-11",
        },
        {
            "universe": "u",
            "portfolio": "T",
            "tag": "margin_batch",
            "method": "fhs",
            "horizon_days": 2,
            "n_runs": 1,
            "first": "2024-01-12",
            "last": "2024-01-12",
        },
    ]


def test_readers_ignore_margin_shaped_runs(seeded: psycopg.Connection[Any], client: Any) -> None:
    """Regression guard (JK, 2026-09-15): the newest row in risk_runs is a 2-day margin run.

    latest_run with tag=None, headline_series and the API must all return the 1-day risk
    series only. If this test fails, a reader lost its positive horizon selection.
    """
    newest = seeded.execute("SELECT max(run_id), max(as_of_date) FROM risk_runs").fetchone()
    assert newest is not None and str(newest[1]) == "2024-01-12"  # the margin run is newest
    run = queries.latest_run(seeded, "u", "T", tag=None)
    assert run is not None and run["as_of"] == "2024-01-11" and run["horizon_days"] == 1
    assert run["run_id"] < newest[0]
    assert [x["as_of"] for x in queries.headline_series(seeded, "u", "T")] == [
        "2024-01-10",
        "2024-01-11",
    ]
    es = client.get("/es", params={"universe": "u", "portfolio": "T"}).json()
    assert es["value"] == 950.0 and es["as_of"] == "2024-01-11"
    # the margin series is reachable only by asking for it
    m = queries.latest_run(seeded, "u", "T", tag="margin_batch", horizon_days=2)
    assert m is not None and m["as_of"] == "2024-01-12"
    assert {x["measure"] for x in m["measures"]} >= {"im", "im_core"}


def test_series_backtest_stress(client: Any) -> None:
    s = client.get("/series", params={"universe": "u", "portfolio": "T"}).json()
    assert [x["var_99"] for x in s] == [800.0, 820.0] and s[0]["stressed_es"] == 1500.0
    bt = client.get("/backtest", params={"universe": "u", "portfolio": "T"}).json()
    assert bt["horizon_method"] == "block"  # newest batch, not the earlier raw one
    assert len(bt["windows"]) == 1 and bt["windows"][0]["exceptions_raw"] == 1
    t = bt["totals"]
    assert (t["days"], t["exceptions"], t["exceptions_raw"], t["exceptions_sqrt"]) == (2, 0, 1, 0)
    assert (t["multi_day_transitions"], t["multi_day_exceptions_raw"]) == (1, 1)
    assert t["expected"] == pytest.approx(0.02)
    days = client.get(
        "/backtest/days", params={"universe": "u", "portfolio": "T", "exceptions_only": "true"}
    ).json()
    assert len(days) == 1 and days[0]["h"] == 2 and days[0]["var_h_block"] == 1200.0
    assert days[0]["attribution"] == {"WTI": 700.0, "_total": 900.0}
    st = client.get("/stress", params={"universe": "u", "portfolio": "T"}).json()
    assert [r["scenario"] for r in st["results"]] == ["independent", "gfc_2008"]
    assert st["results"][1]["loss_over_es"] == 5.0
    assert client.get("/stress", params={"universe": "u", "portfolio": "X"}).status_code == 404


def test_queries_without_rows(seeded: psycopg.Connection[Any]) -> None:
    assert queries.latest_run(seeded, "u", "T", method="mc") is None
    assert queries.backtest_latest(seeded, "u", "X") is None
    assert queries.headline_series(seeded, "u", "T", start=date(2030, 1, 1)) == []


def test_dashboard_smoke(seeded: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    apptest = pytest.importorskip("streamlit.testing.v1")
    schema = seeded.execute("SHOW search_path").fetchone()
    assert schema is not None
    # point the dashboard's own connections at the throwaway schema via the URL
    url = os.environ["DATABASE_URL"]
    sep = "&" if "?" in url else "?"
    monkeypatch.setenv("DATABASE_URL", f"{url}{sep}options=-c%20search_path%3D{schema[0]}")
    at = apptest.AppTest.from_file(
        str(REPO_ROOT / "src" / "risk_engine" / "app" / "dashboard.py"), default_timeout=60
    )
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("950" in m.value for m in at.metric)
    assert any("Backtest" in h.value for h in at.subheader)


def test_margin_endpoints(client: Any) -> None:
    m = client.get("/margin", params={"universe": "u", "portfolio": "T"}).json()
    assert m["as_of"] == "2024-01-12" and m["horizon_days"] == 2 and m["tag"] == "margin_batch"
    assert m["im"] == {"im": 12000.0, "im_core": 11000.0}
    assert client.get("/margin", params={"universe": "u", "portfolio": "X"}).status_code == 404
    cv = client.get("/margin/coverage", params={"universe": "u", "portfolio": "T"}).json()
    assert cv["totals"]["days"] == 1 and cv["totals"]["breaches"] == 1
    assert cv["totals"]["coverage"] == 0.0 and cv["windows"][0]["target"] == 0.99
    assert cv["params"]["margin_params_sha256"] == "m" * 64
    days = client.get(
        "/margin/coverage/days", params={"universe": "u", "portfolio": "T", "breaches_only": 1}
    ).json()
    assert len(days) == 1 and days[0]["shortfall"] == 1000.0 and days[0]["breach_span"] is False
    dfr = client.get("/default-fund", params={"universe": "u", "basis": "path"}).json()
    assert dfr["default_fund"] == 5000.0 and dfr["binding_members"] == ["T", "U"]
    assert [r["uncovered"] for r in dfr["results"]] == [3000.0, 2000.0]
    assert dfr["basis"] == "path"  # a run recorded without a basis counts as path
    assert client.get("/default-fund", params={"universe": "u"}).status_code == 404  # no mpor run
    assert client.get("/default-fund", params={"universe": "u", "basis": "x"}).status_code == 422
    assert client.get("/default-fund", params={"universe": "nope"}).status_code == 404
