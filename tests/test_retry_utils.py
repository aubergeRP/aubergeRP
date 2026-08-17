"""Tests for the shared connector retry policy."""

from __future__ import annotations

import httpx
import pytest

from aubergeRP.utils.retry import (
    ConnectorHTTPError,
    backoff_delays,
    is_retryable_error,
    retry_async,
)


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://example.test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


# ── backoff_delays ────────────────────────────────────────────────────────────

def test_backoff_delays_zero_retries():
    assert backoff_delays(0) == ()


def test_backoff_delays_doubles_and_caps(monkeypatch):
    monkeypatch.setattr("aubergeRP.utils.retry.RETRY_BASE_DELAY", 1.0)
    monkeypatch.setattr("aubergeRP.utils.retry.RETRY_MAX_DELAY", 30.0)
    assert backoff_delays(7) == (1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0)


def test_backoff_delays_negative_is_empty():
    assert backoff_delays(-2) == ()


# ── is_retryable_error ────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_http_statuses_are_retryable(status):
    assert is_retryable_error(_status_error(status)) is True
    assert is_retryable_error(ConnectorHTTPError("boom", status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_definitive_http_statuses_are_not_retryable(status):
    assert is_retryable_error(_status_error(status)) is False
    assert is_retryable_error(ConnectorHTTPError("boom", status)) is False


def test_network_errors_are_retryable():
    request = httpx.Request("GET", "http://example.test")
    assert is_retryable_error(httpx.ConnectError("refused", request=request)) is True
    assert is_retryable_error(httpx.ReadTimeout("slow", request=request)) is True
    assert is_retryable_error(OSError("broken pipe")) is True


def test_unrelated_errors_are_not_retryable():
    assert is_retryable_error(ValueError("bad prompt")) is False


# ── retry_async ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retry_async_recovers_after_transient_failures():
    calls = {"n": 0}

    async def attempt() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _status_error(503)
        return "ok"

    assert await retry_async(attempt, 3, label="test") == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retry_async_gives_up_after_max_retries():
    calls = {"n": 0}

    async def attempt() -> str:
        calls["n"] += 1
        raise _status_error(500)

    with pytest.raises(httpx.HTTPStatusError):
        await retry_async(attempt, 2, label="test")
    # Initial attempt plus two retries.
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retry_async_does_not_retry_definitive_errors():
    calls = {"n": 0}

    async def attempt() -> str:
        calls["n"] += 1
        raise _status_error(401)

    with pytest.raises(httpx.HTTPStatusError):
        await retry_async(attempt, 5, label="test")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_retry_async_with_zero_retries_calls_once():
    calls = {"n": 0}

    async def attempt() -> str:
        calls["n"] += 1
        raise _status_error(500)

    with pytest.raises(httpx.HTTPStatusError):
        await retry_async(attempt, 0, label="test")
    assert calls["n"] == 1
