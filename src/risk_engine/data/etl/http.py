"""Shared HTTP helpers for every fetcher (design note 03 §15).

Two things every source needs and none should re-implement:

* :func:`fetch_with_retry` — GET with the retry rule the source docstrings promise:
  5xx, 429 and timeouts are retried with exponential back-off; other 4xx fail at once.
  ``httpx.HTTPTransport(retries=N)`` only retries *connection* failures, so it cannot
  express this rule; the loop lives here, once, and its numbers are pinned by
  ``tests/test_config_pins.py``.
* :func:`client_for` — the lifetime rule for the optional injected client: a borrowed
  client is used as-is and never closed by the source; when none is injected, one client is
  created for the duration of a single ``fetch`` call and closed on exit.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

#: HTTP status codes that are retried. 429 is included; every other 4xx is final.
RETRY_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

#: User-Agent sent by the default client. home.treasury.gov sits behind a WAF that never
#: answers non-browser strings (``python-httpx/0.28.1``, ``curl/8``, a descriptive
#: ``risk-engine/0.1 (+url)`` all hit ReadTimeout) but accepts a Firefox-format string with
#: our tool name inside the platform parentheses [확인 2026-09-13: 6 variants tested, see
#: docs/decisions.md]. The token ``risk-engine/0.1`` keeps the client identifiable.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.0; rv:130.0; risk-engine/0.1) "
    "Gecko/20100101 Firefox/130.0"
)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry schedule. Frozen so that no caller can adjust it after a failure (note 00 §3).

    Attributes:
        attempts: Total attempts including the first one.
        backoff_seconds: Wait before the second attempt; doubled after each further failure.
        timeout_seconds: Per-request timeout passed to httpx (connect + read).
    """

    attempts: int = 3
    backoff_seconds: float = 1.0
    timeout_seconds: float = 30.0


DEFAULT_RETRY_POLICY = RetryPolicy()


class RetryExhaustedError(RuntimeError):
    """Raised when every attempt failed with a retryable error. Carries the last exception."""


@contextmanager
def client_for(
    injected: httpx.Client | None, policy: RetryPolicy = DEFAULT_RETRY_POLICY
) -> Iterator[httpx.Client]:
    """Yield a usable client following the lifetime rule.

    * ``injected`` given: yield it unchanged and do **not** close it on exit — the caller
      (tests, or a future orchestrator sharing one client) owns it.
    * ``injected`` is ``None``: create ``httpx.Client(timeout=policy.timeout_seconds)``,
      yield it, close it on exit even if an exception propagates.
    """
    if injected is not None:
        yield injected
        return
    with httpx.Client(timeout=policy.timeout_seconds, headers={"User-Agent": USER_AGENT}) as client:
        yield client


def fetch_with_retry(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, str] | None = None,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """GET ``url`` and return the response once it is 2xx, retrying per ``policy``.

    Retry on: any status in :data:`RETRY_STATUSES`, ``httpx.TimeoutException``,
    ``httpx.TransportError``. Wait ``backoff_seconds * 2**(k-1)`` before attempt ``k+1``
    (1 s, 2 s for the default policy), via ``sleep`` so tests can capture the schedule.

    Fail immediately (no retry) on any other 4xx by raising ``httpx.HTTPStatusError``.
    After ``policy.attempts`` retryable failures raise :class:`RetryExhaustedError` from the
    last error. Never logs the URL query string (EIA keys live there); log the path only.
    """
    path = httpx.URL(url).path
    last_error: Exception | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            response = client.get(url, params=params)
        except httpx.TransportError as exc:  # timeouts are a subclass of TransportError
            last_error = exc
            log.warning(
                "GET %s attempt %d/%d: %s", path, attempt, policy.attempts, type(exc).__name__
            )
        else:
            if response.is_success:
                return response
            if response.status_code not in RETRY_STATUSES:
                response.raise_for_status()  # final 4xx: raises HTTPStatusError
            last_error = httpx.HTTPStatusError(
                f"HTTP {response.status_code} for {path}",
                request=response.request,
                response=response,
            )
            log.warning(
                "GET %s attempt %d/%d: HTTP %d",
                path,
                attempt,
                policy.attempts,
                response.status_code,
            )
        if attempt < policy.attempts:
            sleep(policy.backoff_seconds * 2 ** (attempt - 1))
    raise RetryExhaustedError(f"GET {path} failed after {policy.attempts} attempts") from last_error
