# ETL test fixtures

Small, real slices of each source's native format, used to test the parsers without
network access. All three sources permit this (see design note 02 §2):

| File | Source | Provenance | Notes |
|---|---|---|---|
| `fred_dgs10_2020-03_04.json` | FRED, series DGS10 (Board of Governors H.15; "Public Domain: Citation Requested") | Values from FRED's `fredgraph.csv` for 2020-03-02..2020-04-30 (real), wrapped in the API v2 `observations` envelope per the FRED API documentation | Replace with a captured `/fred/series/observations` response once `FRED_API_KEY` is set; 2020-04-10 (Good Friday) is `"."`, FRED's missing-value marker |
| `ecb_eurofxref-hist_2020-03_04.csv` | European Central Bank, Euro foreign exchange reference rates (reuse permitted with attribution: "Source: ECB") | Rows for March–April 2020 cut from `eurofxref-hist.zip`, downloaded 2026-09-12 | Values are `1 EUR = x CCY`; `N/A` marks missing; **every line ends with a trailing comma**, which yields an empty last column |
| `eia_rwtc_2020-04.json` | U.S. Energy Information Administration, WTI Cushing spot (public domain; "Source: U.S. Energy Information Administration") | Captured from the API v2 endpoint `/v2/petroleum/pri/spt/data/` on 2026-09-12 (apiVersion 2.1.13), 2020-04-13..2020-05-01 | `value` is a **string** and drops trailing zeros (`12.4`); includes the negative close of 2020-04-20 (`-36.98`); the echoed `request.params.api_key` has been replaced by `<redacted>` — the live response contains the key, so cached raw responses must be scrubbed before they are written to disk |
