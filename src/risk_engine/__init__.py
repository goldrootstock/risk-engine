"""Portfolio Risk & CCP Margin Engine.

Sub-packages:
    data:     PostgreSQL schema, migrations, ETL (instruments / prices / positions / risk_runs).
    risk:     Return matrix, filtered historical simulation, ES 97.5% / VaR 99%.
    backtest: Kupiec POF, Christoffersen independence, Basel traffic light, PLA-style tests.
"""

__version__ = "0.1.0"
