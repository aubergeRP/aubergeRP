"""Shared retry policy for every connector.

One place defines how aubergeRP waits between attempts: an exponential backoff
that doubles at each retry, starting at one second and capped at 30s
(1, 2, 4, 8, 16, 30, 30 …).  Connectors expose ``max_retries`` in their config;
the delays are derived from it here so every backend behaves the same.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

#: First delay, in seconds. Tests monkeypatch this to 0 to avoid real waits.
RETRY_BASE_DELAY = 1.0
#: Delays double at each retry but never exceed this value.
RETRY_MAX_DELAY = 30.0

class ConnectorHTTPError(ValueError):
    """An HTTP error response from a backend, carrying its status code."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def backoff_delays(max_retries: int) -> tuple[float, ...]:
    """Return one delay per retry: 1, 2, 4, 8, 16, 30, 30 … (capped)."""
    delays = []
    delay = RETRY_BASE_DELAY
    for _ in range(max(0, max_retries)):
        delays.append(min(delay, RETRY_MAX_DELAY))
        delay *= 2
    return tuple(delays)


def is_retryable_error(exc: BaseException) -> bool:
    """Return True for transient failures worth retrying.

    Network errors, timeouts, HTTP 429 and 5xx are transient. Other 4xx
    responses (bad request, invalid API key) are definitive and fail fast.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return _is_retryable_status(exc.response.status_code)
    if isinstance(exc, ConnectorHTTPError):
        return _is_retryable_status(exc.status_code)
    return isinstance(exc, httpx.TransportError | OSError)


def _is_retryable_status(status: int) -> bool:
    return status == 429 or status >= 500


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    max_retries: int,
    *,
    label: str,
) -> T:
    """Await ``fn()``, retrying transient failures with exponential backoff."""
    delays = backoff_delays(max_retries)
    for attempt, delay in enumerate(delays, start=1):
        try:
            return await fn()
        except Exception as exc:
            if not is_retryable_error(exc):
                raise
            logger.warning(
                "%s failed (%s), retrying in %.0fs (attempt %d/%d)",
                label,
                exc,
                delay,
                attempt,
                len(delays) + 1,
            )
            await asyncio.sleep(delay)
    return await fn()
