"""fetch() tests through httpx.MockTransport — no network."""

import io
import json
import zipfile
from datetime import date

import httpx
import pytest

from risk_engine.data.etl.sources.ecb import HIST_URL, SCOPE_ALL, EcbSource
from risk_engine.data.etl.sources.eia import REDACTED, EiaSource, scrub_api_key
from risk_engine.data.etl.sources.fred import LIMIT, OBSERVATIONS_PATH, FredSource


def _client(handler) -> httpx.Client:  # type: ignore[no-untyped-def]
    return httpx.Client(transport=httpx.MockTransport(handler))


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("eurofxref-hist.csv", "Date,USD,\n2020-04-30,1.0876,\n")
    return buf.getvalue()


def test_ecb_fetch_returns_single_all_scope_file() -> None:
    payload = _zip_bytes()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=payload)

    files = EcbSource(client=_client(handler)).fetch(["USD", "JPY"], None, None)
    assert seen == [HIST_URL]
    assert len(files) == 1
    (raw,) = files
    assert (raw.source, raw.scope, raw.url, raw.page, raw.pages) == (
        "ecb",
        SCOPE_ALL,
        HIST_URL,
        1,
        1,
    )
    assert raw.content == payload


def test_ecb_fetch_rejects_html_error_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"<!DOCTYPE html><html>not found</html>")

    with pytest.raises((httpx.HTTPStatusError, ValueError)):
        EcbSource(client=_client(handler)).fetch(["USD"], None, None)


def test_ecb_fetch_rejects_non_zip_body_with_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<!DOCTYPE html>")

    with pytest.raises(ValueError):
        EcbSource(client=_client(handler)).fetch(["USD"], None, None)


def test_eia_scrub_removes_key_from_bytes() -> None:
    body = b'{"request":{"params":{"api_key":"SECRET123"}},"response":{"data":[]}}'
    out = scrub_api_key(body, "SECRET123")
    assert b"SECRET123" not in out
    assert REDACTED.encode() in out


def test_eia_fetch_paginates_and_scrubs() -> None:
    key = "SECRET123"
    pages = {
        "0": {
            "response": {
                "total": "6",
                "data": [
                    {"period": f"2020-04-{d:02d}", "series": "RWTC", "value": "1"}
                    for d in range(20, 25)
                ],
            },
            "request": {"params": {"api_key": key}},
        },
        "5": {
            "response": {
                "total": "6",
                "data": [{"period": "2020-04-27", "series": "RWTC", "value": "1"}],
            },
            "request": {"params": {"api_key": key}},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Api-Key"] == key  # header auth, never the query string
        assert "api_key" not in request.url.params
        return httpx.Response(200, json=pages[request.url.params.get("offset", "0")])

    src = EiaSource(api_key=key, client=_client(handler))
    files = src.fetch(["RWTC"], date(2020, 4, 20), date(2020, 4, 27))
    assert [(f.scope, f.page, f.pages) for f in files] == [("RWTC", 1, 2), ("RWTC", 2, 2)]
    for f in files:
        assert key.encode() not in f.content
        assert json.loads(f.content)["request"]["params"]["api_key"] == REDACTED
        assert "?" not in f.url and "api_key" not in f.url


def test_fred_fetch_one_request_per_series_without_query_in_url() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(
            200,
            json={
                "count": 2,
                "observations": [
                    {"date": "2020-04-20", "value": "0.63"},
                    {"date": "2020-04-21", "value": "."},
                ],
            },
        )

    files = FredSource(api_key="SECRET", client=_client(handler)).fetch(
        ["DGS10", "DGS2"], date(2020, 4, 20), date(2020, 4, 21)
    )
    assert [u.path for u in seen] == [OBSERVATIONS_PATH, OBSERVATIONS_PATH]
    assert seen[0].params["series_id"] == "DGS10" and seen[0].params["api_key"] == "SECRET"
    assert seen[0].params["limit"] == str(LIMIT)
    assert seen[0].params["observation_start"] == "2020-04-20"
    assert [(f.scope, f.page, f.pages) for f in files] == [("DGS10", 1, 1), ("DGS2", 1, 1)]
    assert all("?" not in f.url and "SECRET" not in f.url for f in files)


def test_fred_fetch_rejects_payload_without_observations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error_code": 400, "error_message": "Bad Request"})

    with pytest.raises(ValueError):
        FredSource(api_key="k", client=_client(handler)).fetch(["DGS10"], None, None)
