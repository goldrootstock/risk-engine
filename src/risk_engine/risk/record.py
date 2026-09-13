"""Persist a :class:`~risk_engine.risk.engine.RunResult` (design note 01 §8, 05 §5).

The only writer of ``risk_runs`` / ``risk_measures``; one transaction per run.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from risk_engine.risk.engine import RunResult

INSERT_RUN = """
INSERT INTO risk_runs (portfolio_code, as_of_date, positions_as_of, method, horizon_days,
                       window_days, n_scenarios, portfolio_value, params, tag, code_version,
                       universe)
VALUES (%(portfolio_code)s, %(as_of_date)s, %(positions_as_of)s, %(method)s, %(horizon_days)s,
        %(window_days)s, %(n_scenarios)s, %(portfolio_value)s, %(params)s, %(tag)s,
        %(code_version)s, %(universe)s)
RETURNING run_id
"""

INSERT_MEASURE = """
INSERT INTO risk_measures (run_id, measure, confidence, scope_type, scope_key, value)
VALUES (%s, %s, %s, %s, %s, %s)
"""


def code_version() -> str | None:
    """Short git SHA of the checkout, or ``None``."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def write(
    conn: psycopg.Connection[Any], result: RunResult, code_version_: str | None = None
) -> int:
    """Insert the run header and its measures atomically; returns ``run_id``."""
    row = {
        "portfolio_code": result.portfolio_code,
        "as_of_date": result.as_of_date,
        "positions_as_of": result.positions_as_of,
        "method": result.method,
        "horizon_days": result.horizon_days,
        "window_days": result.window_days,
        "n_scenarios": result.n_scenarios,
        "portfolio_value": result.portfolio_value,
        "params": Jsonb(result.params, dumps=lambda o: json.dumps(o, default=str)),
        "tag": result.tag,
        "code_version": code_version_,
        "universe": str(result.params.get("universe", "default")),
    }
    with conn.transaction():
        got = conn.execute(INSERT_RUN, row).fetchone()
        assert got is not None
        run_id = int(got[0])
        with conn.cursor() as cur:
            cur.executemany(
                INSERT_MEASURE,
                [
                    (run_id, m.measure, m.confidence, m.scope_type, m.scope_key, m.value)
                    for m in result.measures
                ],
            )
    return run_id
