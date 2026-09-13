"""Streamlit dashboard over recorded results (design note 08 §4).

``streamlit run src/risk_engine/app/dashboard.py``. Reads through the same
:mod:`risk_engine.app.queries` as the API, on a read-only connection; never computes.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from risk_engine.app import queries
from risk_engine.app.api import connect_read_only
from risk_engine.settings import load_settings

KIND_ORDER = ["historical", "hypothetical", "correlation", "vol_lag"]
ZONE_COLOURS = {"green": "#2e7d32", "yellow": "#f9a825", "amber": "#f9a825", "red": "#c62828"}


@st.cache_data(ttl=60)
def _catalog(url: str) -> list[dict[str, Any]]:
    with connect_read_only(url) as conn:
        return queries.catalog(conn)


@st.cache_data(ttl=60)
def _latest_run(url: str, universe: str, portfolio: str, tag: str) -> dict[str, Any] | None:
    with connect_read_only(url) as conn:
        return queries.latest_run(conn, universe, portfolio, tag=tag)


@st.cache_data(ttl=60)
def _series(url: str, universe: str, portfolio: str, tag: str) -> list[dict[str, Any]]:
    with connect_read_only(url) as conn:
        return queries.headline_series(conn, universe, portfolio, tag=tag)


@st.cache_data(ttl=60)
def _backtest(url: str, universe: str, portfolio: str) -> dict[str, Any] | None:
    with connect_read_only(url) as conn:
        return queries.backtest_latest(conn, universe, portfolio)


@st.cache_data(ttl=60)
def _days(url: str, universe: str, portfolio: str) -> list[dict[str, Any]]:
    with connect_read_only(url) as conn:
        return queries.backtest_days(conn, universe, portfolio)


@st.cache_data(ttl=60)
def _stress(url: str, universe: str, portfolio: str) -> dict[str, Any] | None:
    with connect_read_only(url) as conn:
        return queries.stress_latest(conn, universe, portfolio)


def _money(x: float | None) -> str:
    return "—" if x is None else f"{x:,.0f}"


def _headline_section(run: dict[str, Any]) -> None:
    m = {(x["measure"], x["confidence"]): x["value"] for x in run["measures"]}
    es_975, var_99 = m.get(("es", 0.975)), m.get(("var", 0.99))
    stressed = next((v for (k, _), v in m.items() if k == "stressed_es"), None)
    st.subheader(f"Headline — {run['method']} run {run['run_id']} as of {run['as_of']}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("ES 97.5 %", _money(es_975))
    c2.metric("VaR 99 %", _money(var_99))
    c3.metric("Stressed ES", _money(stressed))
    ratio = f"{es_975 / var_99:.3f}" if es_975 and var_99 else "—"
    c4.metric("ES / VaR", ratio, help="normal theory 1.005; fat tails push it up")
    st.caption(
        f"portfolio value {run['portfolio_value']:,.0f} {run['base_currency']} · positions as of "
        f"{run['positions_as_of']} · window {run['window_days']} · scenarios {run['n_scenarios']}"
        f" · code {run['code_version'] or '?'} · risk_params sha "
        f"{str((run['params'] or {}).get('risk_params_sha256', ''))[:12]}"
    )
    comp = run["component_es"]
    if comp:
        df = pd.DataFrame({"instrument": list(comp), "component_es": list(comp.values())})
        df = df.sort_values("component_es", ascending=False)
        chart = (
            alt.Chart(df)
            .mark_bar()
            .encode(
                x=alt.X("component_es:Q", title="component ES (Euler, sums to ES)"),
                y=alt.Y("instrument:N", sort="-x", title=None),
                tooltip=["instrument", alt.Tooltip("component_es:Q", format=",.0f")],
            )
            .properties(height=max(120, 18 * len(df)))
        )
        st.altair_chart(chart, width="stretch")


def _series_section(series: list[dict[str, Any]], days: list[dict[str, Any]]) -> None:
    st.subheader("ES / VaR over time and backtest exceptions")
    if not series:
        st.info("no daily_batch series for this selection")
        return
    df = pd.DataFrame(series)
    df["as_of"] = pd.to_datetime(df["as_of"])
    long = df.melt(
        id_vars=["as_of"], value_vars=["es_975", "var_99", "stressed_es"], var_name="measure"
    )
    lines = (
        alt.Chart(long)
        .mark_line()
        .encode(
            x=alt.X("as_of:T", title=None),
            y=alt.Y("value:Q", title="loss, base currency"),
            color="measure:N",
        )
    )
    layers: list[Any] = [lines]
    exc = [d for d in days if d["exception"]]
    if exc:
        ex = pd.DataFrame(exc)
        ex["as_of"] = pd.to_datetime(ex["as_of"])
        ex["loss"] = -ex["hpl"]  # sign flip #1 lives in the backtest; this is a chart copy
        layers.append(
            alt.Chart(ex)
            .mark_point(color="#c62828", size=60, filled=True)
            .encode(
                x="as_of:T",
                y="loss:Q",
                tooltip=[
                    alt.Tooltip("as_of:T"),
                    alt.Tooltip("loss:Q", format=",.0f"),
                    alt.Tooltip("var_99:Q", format=",.0f"),
                    "h:Q",
                ],
            )
        )
    st.altair_chart(alt.layer(*layers).properties(height=280), width="stretch")

    if days:
        pnl = pd.DataFrame(days)
        pnl["as_of"] = pd.to_datetime(pnl["as_of"])
        # Sign convention (CLAUDE.md §2): VaR is stored positive; the VaR band on a P&L chart
        # is the second of the two places where the sign is flipped.
        pnl["neg_var"] = -pnl["var_99"]
        pnl["neg_var_h"] = -pnl["var_h_block"].fillna(pnl["var_99"])
        base = alt.Chart(pnl).encode(x=alt.X("as_of:T", title=None))
        chart = alt.layer(
            base.mark_point(size=8, opacity=0.5).encode(
                y=alt.Y("hpl:Q", title="hypothetical P&L (profit > 0)"),
                color=alt.condition("datum.exception", alt.value("#c62828"), alt.value("#607d8b")),
                tooltip=[
                    alt.Tooltip("as_of:T"),
                    alt.Tooltip("hpl:Q", format=",.0f"),
                    alt.Tooltip("var_99:Q", format=",.0f"),
                    "h:Q",
                    "exception:N",
                    "exception_raw:N",
                ],
            ),
            base.mark_line(color="#1565c0", strokeWidth=1).encode(y="neg_var:Q"),
            base.mark_line(color="#1565c0", strokeWidth=1, strokeDash=[3, 3]).encode(
                y="neg_var_h:Q"
            ),
        ).properties(height=260)
        st.altair_chart(chart, width="stretch")
        st.caption("solid: -VaR 99 (1-day); dashed: -(h-day block VaR) where the gap is >= 2 days")


def _backtest_section(bt: dict[str, Any] | None) -> None:
    st.subheader("Backtest — newest batch")
    if bt is None:
        st.info("no backtest recorded for this selection")
        return
    t = bt["totals"]
    st.caption(
        f"official exception = **{bt['horizon_method'] or 'raw'}** horizon · {t['first']} → "
        f"{t['last']} · batch {bt['created_at'][:19]} · code {bt['code_version'] or '?'}"
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("days", f"{t['days']:,}")
    c2.metric("exceptions (official)", t["exceptions"], help=f"expected {t['expected']:.1f}")
    c3.metric("raw (1-day VaR)", t["exceptions_raw"])
    c4.metric("√h", t["exceptions_sqrt"])
    st.caption(
        f"transitions spanning ≥ 2 business days: {t['multi_day_transitions']} — exceptions "
        f"official {t['multi_day_exceptions']}, raw {t['multi_day_exceptions_raw']}"
    )
    df = pd.DataFrame(bt["windows"])
    cols = [
        "window_start",
        "window_end",
        "n_obs",
        "exceptions",
        "exceptions_raw",
        "exceptions_sqrt",
        "expected",
        "traffic_light",
        "kupiec_p",
        "christoffersen_p",
        "cc_p",
        "pla_spearman",
        "pla_ks",
        "pla_zone",
    ]
    st.dataframe(
        df[cols].style.format(
            {
                "expected": "{:.1f}",
                "kupiec_p": "{:.3f}",
                "christoffersen_p": "{:.3f}",
                "cc_p": "{:.3f}",
                "pla_spearman": "{:.3f}",
                "pla_ks": "{:.3f}",
            }
        ),
        width="stretch",
        hide_index=True,
    )


def _stress_section(stress: dict[str, Any] | None) -> None:
    st.subheader("Stress — newest run")
    if stress is None:
        st.info("no stress run recorded for this selection")
        return
    st.caption(
        f"as of {stress['as_of']} · ES 97.5 reference {stress['es_975']:,.0f} · scenario set "
        f"{stress['scenario_set_sha256'][:12]} · code {stress['code_version'] or '?'}"
    )
    df = pd.DataFrame(stress["results"])
    df["x_es"] = df["loss"] / stress["es_975"]
    chart = (
        alt.Chart(df)
        .mark_bar()
        .encode(
            x=alt.X("x_es:Q", title="loss / ES 97.5"),
            y=alt.Y("scenario:N", sort="-x", title=None),
            color=alt.Color("kind:N", sort=KIND_ORDER),
            tooltip=[
                "scenario",
                "kind",
                alt.Tooltip("loss:Q", format=",.0f"),
                alt.Tooltip("x_es:Q", format=".2f"),
                "window_start",
                "window_end",
                "worst_day",
            ],
        )
        .properties(height=max(160, 16 * len(df)))
    )
    st.altair_chart(chart, width="stretch")


def main() -> None:
    """Page body."""
    st.set_page_config(page_title="risk-engine", layout="wide")
    st.title("risk-engine — recorded results")
    url = load_settings().database_url
    cat = _catalog(url)
    if not cat:
        st.warning("no runs recorded yet — run the CLI first")
        return
    universes = sorted({c["universe"] for c in cat})
    with st.sidebar:
        universe = st.selectbox(
            "universe",
            universes,
            index=universes.index("from_1999") if "from_1999" in universes else 0,
        )
        portfolios = sorted({c["portfolio"] for c in cat if c["universe"] == universe})
        portfolio = st.selectbox("portfolio", portfolios)
        tags = sorted({c["tag"] for c in cat if c["universe"] == universe})
        tag = st.selectbox(
            "tag", tags, index=tags.index("daily_batch") if "daily_batch" in tags else 0
        )
        st.caption("read-only view; results are written by the CLI only")
        st.caption(f"today {date.today().isoformat()}")

    run = _latest_run(url, universe, portfolio, tag)
    if run is None:
        st.info("no run for this selection")
    else:
        _headline_section(run)
    days = _days(url, universe, portfolio)
    _series_section(_series(url, universe, portfolio, tag), days)
    _backtest_section(_backtest(url, universe, portfolio))
    _stress_section(_stress(url, universe, portfolio))


main()
