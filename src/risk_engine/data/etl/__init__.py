"""ETL: vendor sources -> raw cache -> validated frames -> `prices`.

Layout (design note 03):
    contract   types shared by every stage (RawFile, Source, Finding, Report, LoadResult)
    sources    one fetch/parse pair per vendor (ecb, ustreasury, eia)
    cache      append-only raw-byte cache with manifest
    validate   read-only checks returning a Report
    load       upserts into instruments / prices and the etl_runs record
    __main__   `python -m risk_engine.data.etl sync|status`
"""
