"""Persist stress runs (the only writer of ``stress_runs`` / ``stress_results``)."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from risk_engine.risk.stress import StressResult

INSERT_RUN = """
INSERT INTO stress_runs (portfolio_code, universe, as_of_date, positions_as_of, portfolio_value,
                         es_975, scenario_set_sha256, params, code_version)
VALUES (%(portfolio_code)s, %(universe)s, %(as_of_date)s, %(positions_as_of)s,
        %(portfolio_value)s, %(es_975)s, %(sha)s, %(params)s, %(code_version)s)
RETURNING stress_run_id
"""

INSERT_RESULT = """
INSERT INTO stress_results (stress_run_id, scenario, kind, window_start, window_end, loss,
                            worst_day, worst_day_loss, loss_over_es, attribution)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def write(
    conn: psycopg.Connection[Any],
    *,
    portfolio_code: str,
    universe: str,
    as_of: date,
    positions_as_of: date,
    portfolio_value: float,
    es_975: float,
    scenario_sha256: str,
    params: dict[str, Any],
    results: list[StressResult],
    code_version: str | None,
) -> int:
    """Insert the header and all results atomically; returns ``stress_run_id``."""
    with conn.transaction():
        got = conn.execute(
            INSERT_RUN,
            {
                "portfolio_code": portfolio_code,
                "universe": universe,
                "as_of_date": as_of,
                "positions_as_of": positions_as_of,
                "portfolio_value": portfolio_value,
                "es_975": es_975,
                "sha": scenario_sha256,
                "params": Jsonb(params, dumps=lambda o: json.dumps(o, default=str)),
                "code_version": code_version,
            },
        ).fetchone()
        assert got is not None
        run_id = int(got[0])
        with conn.cursor() as cur:
            cur.executemany(
                INSERT_RESULT,
                [
                    (
                        run_id,
                        r.scenario,
                        r.kind,
                        r.window_start,
                        r.window_end,
                        r.loss,
                        r.worst_day,
                        r.worst_day_loss,
                        (r.loss / es_975) if es_975 else None,
                        Jsonb({k: round(v, 2) for k, v in r.attribution.items()}),
                    )
                    for r in results
                ],
            )
    return run_id
