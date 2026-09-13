"""Retry/lifetime helper tests through httpx.MockTransport (no network)."""

from collections.abc import Iterator

import httpx
import pytest

from risk_engine.data.etl.http import (
    USER_AGENT,
    RetryExhaustedError,
    RetryPolicy,
    client_for,
    fetch_with_retry,
)

POLICY = RetryPolicy(attempts=3, backoff_seconds=1.0, timeout_seconds=5.0)
URL = "https://example.test/data?api_key=SECRET"


def _client(responses: Iterator[httpx.Response | Exception]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        item = next(responses)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_retries_5xx_then_succeeds_with_exponential_backoff() -> None:
    client = _client(
        iter([httpx.Response(503), httpx.Response(500), httpx.Response(200, content=b"ok")])
    )
    waits: list[float] = []
    response = fetch_with_retry(client, URL, policy=POLICY, sleep=waits.append)
    assert response.content == b"ok"
    assert waits == [1.0, 2.0]


def test_retries_timeout_and_429() -> None:
    client = _client(iter([httpx.ReadTimeout("slow"), httpx.Response(429), httpx.Response(200)]))
    waits: list[float] = []
    assert fetch_with_retry(client, URL, policy=POLICY, sleep=waits.append).status_code == 200
    assert len(waits) == 2


def test_4xx_fails_immediately_without_sleeping() -> None:
    client = _client(iter([httpx.Response(404), httpx.Response(200)]))
    waits: list[float] = []
    with pytest.raises(httpx.HTTPStatusError):
        fetch_with_retry(client, URL, policy=POLICY, sleep=waits.append)
    assert waits == []


def test_exhausted_after_configured_attempts() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RetryExhaustedError):
        fetch_with_retry(client, URL, policy=POLICY, sleep=lambda s: None)
    assert calls["n"] == POLICY.attempts


def test_client_for_borrows_injected_client_without_closing() -> None:
    injected = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with client_for(injected) as client:
        assert client is injected
    assert not injected.is_closed
    injected.close()


def test_client_for_creates_and_closes_default_client() -> None:
    with client_for(None, POLICY) as client:
        created = client
        assert not created.is_closed
        assert created.timeout.read == POLICY.timeout_seconds
    assert created.is_closed


def test_default_client_identifies_the_tool() -> None:
    with client_for(None, POLICY) as client:
        assert client.headers["User-Agent"] == USER_AGENT
    assert "risk-engine/0.1" in USER_AGENT


def test_exception_messages_never_contain_the_query_string() -> None:
    client = _client(iter([httpx.Response(404)]))
    with pytest.raises(httpx.HTTPStatusError) as info:
        fetch_with_retry(client, URL, policy=POLICY, sleep=lambda s: None)
    assert "SECRET" not in str(info.value) and "api_key" not in str(info.value)

    client = _client(iter([httpx.ReadTimeout("slow")] * POLICY.attempts))
    with pytest.raises(RetryExhaustedError) as info2:
        fetch_with_retry(client, URL, policy=POLICY, sleep=lambda s: None)
    chain = f"{info2.value} / {info2.value.__cause__}"
    assert "SECRET" not in chain and "?" not in chain
