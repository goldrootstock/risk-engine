"""CCP-style initial margin (design note 09).

Layout:
    params        frozen ``MarginParams`` from ``config/margin_params.toml`` (sha256 recorded)
    core          FHS ES/VaR at the MPOR, 10-year volatility floor, stress blend, add-ons
    span          legacy SPAN 16-scenario margin for comparison
    engine        one margin run: data -> core + span -> measures (reads only)
    record        the only writer of margin runs (``risk_runs`` / ``risk_measures``)
    coverage      coverage backtest against realised MPOR losses (read-only; own writer)
    default_fund  Cover-N default fund from members' stress losses (read-only; own writer)
"""
