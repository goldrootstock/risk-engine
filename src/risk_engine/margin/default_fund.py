"""Cover-N default fund from the members' stress losses (design note 09 §7, §7-1).

Members are ``positions.portfolio_code`` values. For every scenario of the stress file the
unscaled loss of each member's book (note 07: historical replay / hypothetical shocks) is
compared with the IM recorded for that member on the date; the default fund is the largest,
over scenarios, sum of the ``cover`` biggest uncovered excesses [PFMI Principle 4 KC 4;
EMIR Art. 42(3)]. Shock sizes come only from the scenario file; :func:`cover_n` is pure;
:func:`record` is the only writer of ``default_fund_runs`` / ``default_fund_results``.

Three bases (JK decisions, 2026-09-15):

* ``mpor_historical`` — **official**. Historical scenarios only, each contributing the worst
  ``horizon``-day (MPOR) window inside its date range: the default fund covers the loss over
  the close-out period [PFMI Principle 4: "extreme but plausible market conditions" over the
  period the CCP holds the defaulter's positions]. The hypothetical shocks are excluded by
  *category*, not by size: ``rates_up_200`` and its kind are Basel IRRBB supervisory shocks
  for bank capital, instantaneous and not a close-out loss (the worst 2-day 10-year move in
  the sample is about 50 bp; 200 bp is four times it). They are not rescaled to the MPOR
  either — a rescaled standard shock is no longer the standard.
* ``mpor`` — the same historical windows plus the hypothetical shocks, kept as the
  supervisory-reference view.
* ``path`` — whole-window cumulative change plus the hypothetical shocks, kept as the path /
  liquidity view.

All three are recorded on every run so that choosing the (smaller) official number stays
visible next to the numbers it excludes (note 00 §3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import psycopg
from psycopg.types.json import Jsonb

from risk_engine.data.etl.contract import InstrumentSpec
from risk_engine.margin.core import block_sums
from risk_engine.margin.engine import OFFICIAL_TAG
from risk_engine.risk import stress
from risk_engine.risk.engine import prepare
from risk_engine.risk.measures import to_loss
from risk_engine.risk.pnl import pnl_matrix
from risk_engine.risk.positions import load_snapshot
from risk_engine.risk.returns import ReturnMatrix

BASES: tuple[str, ...] = ("mpor_historical", "mpor", "path")
OFFICIAL_BASIS = "mpor_historical"

MEMBER_IM_SQL = """
SELECT r.run_id, m.value
FROM risk_runs r JOIN risk_measures m USING (run_id)
WHERE r.universe = %(universe)s AND r.portfolio_code = %(portfolio)s AND r.tag = %(tag)s
  AND r.method = 'fhs' AND r.horizon_days = %(mpor)s AND r.as_of_date = %(as_of)s
  AND m.measure = 'im' AND m.scope_type = 'portfolio'
ORDER BY r.run_id DESC LIMIT 1
"""


@dataclass(frozen=True, slots=True)
class MemberIm:
    """The margin run that supplies a member's IM on the date."""

    portfolio_code: str
    im_run_id: int
    im: float


@dataclass(frozen=True, slots=True)
class ScenarioLoss:
    """Unscaled stress loss of one member under one scenario (loss positive).

    ``window_start`` / ``window_end`` name the aligned dates the loss was taken over: the
    whole scenario window on the ``path`` basis, the worst MPOR-length block on the ``mpor``
    basis, nothing for a hypothetical shock.
    """

    scenario: str
    kind: str
    portfolio_code: str
    stress_loss: float
    window_start: date | None = None
    window_end: date | None = None


@dataclass(frozen=True, slots=True)
class MemberResult:
    """One ``default_fund_results`` row."""

    scenario: str
    kind: str
    portfolio_code: str
    im_run_id: int
    stress_loss: float
    im: float
    uncovered: float


@dataclass(frozen=True, slots=True)
class DefaultFundReport:
    """Cover-N sizing plus the full (scenario, member) table."""

    default_fund: float
    binding_scenario: str
    binding_members: tuple[str, ...]
    cover: int
    n_members: int
    rows: tuple[MemberResult, ...]
    by_scenario: dict[str, float]  # top-`cover` uncovered sum per scenario


def cover_n(
    losses: Sequence[ScenarioLoss], ims: Mapping[str, MemberIm], cover: int
) -> DefaultFundReport:
    """Default fund = max over scenarios of the sum of the ``cover`` largest uncovered losses.

    Pure. Every member in ``ims`` must appear under every scenario in ``losses``.
    """
    if cover < 1:
        raise ValueError("cover must be >= 1")
    members = sorted(ims)
    rows: list[MemberResult] = []
    per_scenario: dict[str, list[MemberResult]] = {}
    for sl in losses:
        if sl.portfolio_code not in ims:
            raise KeyError(f"no IM for member {sl.portfolio_code!r}")
        m = ims[sl.portfolio_code]
        r = MemberResult(
            sl.scenario,
            sl.kind,
            sl.portfolio_code,
            m.im_run_id,
            sl.stress_loss,
            m.im,
            max(0.0, sl.stress_loss - m.im),
        )
        rows.append(r)
        per_scenario.setdefault(sl.scenario, []).append(r)
    for scenario, rs in per_scenario.items():
        if sorted(r.portfolio_code for r in rs) != members:
            raise ValueError(f"scenario {scenario!r} does not cover every member")
    by_scenario: dict[str, float] = {}
    binding: tuple[str, tuple[str, ...]] = ("", ())
    best = -1.0
    for scenario, rs in per_scenario.items():
        top = sorted(rs, key=lambda r: -r.uncovered)[:cover]
        total = float(sum(r.uncovered for r in top))
        by_scenario[scenario] = total
        if total > best:
            best, binding = total, (scenario, tuple(r.portfolio_code for r in top))
    if best < 0:
        raise ValueError("no scenarios")
    return DefaultFundReport(
        default_fund=best,
        binding_scenario=binding[0],
        binding_members=binding[1],
        cover=cover,
        n_members=len(members),
        rows=tuple(rows),
        by_scenario=by_scenario,
    )


def load_inputs(
    conn: psycopg.Connection[Any],
    universe: str,
    as_of: date,
    members: Sequence[str],
    *,
    mpor_days: int,
    tag: str = OFFICIAL_TAG,
) -> tuple[
    ReturnMatrix, dict[str, InstrumentSpec], dict[str, dict[str, float]], dict[str, MemberIm]
]:
    """Return matrix, specs, each member's book and each member's recorded IM. Read-only.

    A member without a margin run on ``as_of`` (same universe, tag and MPOR) raises: the
    default fund is sized against *recorded* margins, never against numbers computed here.
    """
    rm, specs, _ = prepare(conn, universe, as_of)
    books: dict[str, dict[str, float]] = {}
    ims: dict[str, MemberIm] = {}
    for code in members:
        books[code], _ = load_snapshot(conn, code, as_of)
        row = conn.execute(
            MEMBER_IM_SQL,
            {
                "universe": universe,
                "portfolio": code,
                "tag": tag,
                "mpor": mpor_days,
                "as_of": as_of,
            },
        ).fetchone()
        if row is None:
            raise LookupError(f"no margin run for {code} on {as_of} (tag {tag}, h={mpor_days})")
        ims[code] = MemberIm(code, int(row[0]), float(row[1]))
    return rm, specs, books, ims


def worst_window_loss(
    rm: ReturnMatrix,
    name: str,
    start: date,
    end: date,
    horizon: int,
    positions: Mapping[str, float],
    specs: Mapping[str, InstrumentSpec],
) -> ScenarioLoss:
    """Worst ``horizon``-day cumulative loss inside ``[start, end]``, unscaled, on today's book.

    Same snapping as :func:`risk_engine.risk.stress.historical` (start forward, end back).
    Blocks are sums of ``horizon`` consecutive change vectors mapped once through the exact
    P&L (as the block bootstrap and the realised MPOR loss are). A window shorter than
    ``horizon`` contributes its whole cumulative change (a loss over fewer days is a valid
    loss over the MPOR).
    """
    idx = rm.changes.index
    a, b = stress._snap(idx, start, "right"), stress._snap(idx, end, "left")
    if b <= a:
        raise ValueError(f"{name}: window {start}..{end} has no aligned observations")
    block = rm.changes.iloc[a + 1 : b + 1]
    arr = block.to_numpy(dtype="float64")
    h = min(horizon, len(arr))
    sums = block_sums(arr, h)
    pnl = pnl_matrix(
        sums, list(rm.changes.columns), rm.levels.iloc[-1], dict(positions), specs, rm.factor_of
    )
    losses = to_loss(pnl).sum(axis=1)
    worst = int(np.argmax(losses))
    return ScenarioLoss(
        scenario=name,
        kind="historical",
        portfolio_code="",
        stress_loss=float(losses[worst]),
        window_start=block.index[worst].date(),
        window_end=block.index[worst + h - 1].date(),
    )


def stress_losses(
    rm: ReturnMatrix,
    books: Mapping[str, Mapping[str, float]],
    specs: Mapping[str, InstrumentSpec],
    scen: stress.ScenarioSet,
    *,
    horizon: int | None = None,
    include_hypothetical: bool = True,
) -> list[ScenarioLoss]:
    """Every (scenario, member) loss from the scenario file, unscaled (note 07 §1-§2).

    ``horizon=None`` takes the whole-window cumulative change; ``horizon=h`` the worst h-day
    block inside each window. ``include_hypothetical=False`` drops the hypothetical shocks
    (the official ``mpor_historical`` basis).
    """
    out: list[ScenarioLoss] = []
    for code, book in books.items():
        for name, spec in scen.historical.items():
            if horizon is None:
                r = stress.historical(rm, name, spec["start"], spec["end"], book, specs)
                out.append(
                    ScenarioLoss(name, "historical", code, r.loss, r.window_start, r.window_end)
                )
            else:
                w = worst_window_loss(rm, name, spec["start"], spec["end"], horizon, book, specs)
                out.append(
                    ScenarioLoss(
                        name, "historical", code, w.stress_loss, w.window_start, w.window_end
                    )
                )
        if not include_hypothetical:
            continue
        for name, spec in scen.hypothetical.items():
            r = stress.hypothetical(rm, name, spec, book, specs)
            out.append(ScenarioLoss(name, "hypothetical", code, r.loss))
    return out


def losses_for_basis(
    rm: ReturnMatrix,
    books: Mapping[str, Mapping[str, float]],
    specs: Mapping[str, InstrumentSpec],
    scen: stress.ScenarioSet,
    basis: str,
    mpor_days: int,
) -> list[ScenarioLoss]:
    """The scenario losses that define ``basis`` (see the module docstring)."""
    if basis == "mpor_historical":
        return stress_losses(rm, books, specs, scen, horizon=mpor_days, include_hypothetical=False)
    if basis == "mpor":
        return stress_losses(rm, books, specs, scen, horizon=mpor_days)
    if basis == "path":
        return stress_losses(rm, books, specs, scen)
    raise ValueError(f"unknown basis {basis!r}; expected one of {BASES}")


INSERT_RUN = """
INSERT INTO default_fund_runs (universe, as_of_date, scenario_set_sha256, margin_params_sha256,
    n_members, cover, default_fund, binding_scenario, binding_members, params, code_version)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
RETURNING default_fund_run_id
"""

INSERT_ROW = """
INSERT INTO default_fund_results (default_fund_run_id, scenario, kind, portfolio_code, im_run_id,
    stress_loss, im, uncovered)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
"""


def record(
    conn: psycopg.Connection[Any],
    report: DefaultFundReport,
    *,
    universe: str,
    as_of: date,
    scenario_sha256: str,
    margin_params_sha256: str,
    params: Mapping[str, Any],
    code_version: str | None = None,
    losses: Sequence[ScenarioLoss] = (),
) -> int:
    """Insert the header and every (scenario, member) row atomically; returns the run id.

    ``params`` must carry ``basis`` (``mpor`` | ``path``); the windows actually used per
    (scenario, member) are stored from ``losses`` under ``params.windows``.
    """
    if params.get("basis") not in BASES:
        raise ValueError(f"params['basis'] must be one of {BASES}")
    windows = {
        f"{sl.scenario}/{sl.portfolio_code}": [
            sl.window_start.isoformat(),
            sl.window_end.isoformat(),
        ]
        for sl in losses
        if sl.window_start is not None and sl.window_end is not None
    }
    with conn.transaction():
        got = conn.execute(
            INSERT_RUN,
            (
                universe,
                as_of,
                scenario_sha256,
                margin_params_sha256,
                report.n_members,
                report.cover,
                report.default_fund,
                report.binding_scenario,
                list(report.binding_members),
                Jsonb({**params, "by_scenario": report.by_scenario, "windows": windows}),
                code_version,
            ),
        ).fetchone()
        assert got is not None
        run_id = int(got[0])
        with conn.cursor() as cur:
            cur.executemany(
                INSERT_ROW,
                [
                    (
                        run_id,
                        r.scenario,
                        r.kind,
                        r.portfolio_code,
                        r.im_run_id,
                        r.stress_loss,
                        r.im,
                        r.uncovered,
                    )
                    for r in report.rows
                ],
            )
    return run_id
