"""Admin authentication router."""

from __future__ import annotations

import os
from collections import deque
from threading import Lock
from time import monotonic

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from ..config import get_config
from ..models.admin import AdminLoginRequest, AdminLoginResponse
from ..utils.auth import create_admin_jwt, verify_admin_jwt, verify_password

router = APIRouter(prefix="/admin", tags=["admin"])

_ADMIN_LOGIN_RATE_LIMIT = 3
_ADMIN_LOGIN_WINDOW_SECONDS = 60.0
_admin_login_failures: dict[str, deque[float]] = {}
_admin_login_failures_lock = Lock()


def _prune_admin_login_failures(failures: deque[float], now: float) -> None:
    cutoff = now - _ADMIN_LOGIN_WINDOW_SECONDS
    while failures and failures[0] <= cutoff:
        failures.popleft()


def _get_admin_login_client_ip(request: Request) -> str:
    cf_connecting_ip = request.headers.get("cf-connecting-ip", "").strip()
    if cf_connecting_ip:
        return cf_connecting_ip

    x_forwarded_for = request.headers.get("x-forwarded-for", "")
    if x_forwarded_for:
        forwarded_ip = x_forwarded_for.split(",")[0].strip()
        if forwarded_ip:
            return forwarded_ip

    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _check_admin_login_rate_limit(client_ip: str) -> None:
    now = monotonic()
    with _admin_login_failures_lock:
        failures = _admin_login_failures.get(client_ip)
        if failures is None:
            return
        _prune_admin_login_failures(failures, now)
        if not failures:
            _admin_login_failures.pop(client_ip, None)
            return
        if len(failures) >= _ADMIN_LOGIN_RATE_LIMIT:
            raise HTTPException(status_code=429, detail="Too many login attempts. Please try again in a minute.")


def _record_admin_login_failure(client_ip: str) -> None:
    now = monotonic()
    with _admin_login_failures_lock:
        failures = _admin_login_failures.setdefault(client_ip, deque())
        _prune_admin_login_failures(failures, now)
        failures.append(now)


def _reset_admin_login_failures(client_ip: str) -> None:
    with _admin_login_failures_lock:
        _admin_login_failures.pop(client_ip, None)


def get_admin_token(x_admin_token: str = Header(default="")) -> str:
    """FastAPI dependency to extract and validate admin auth token.

    Args:
        x_admin_token: The admin token from X-Admin-Token header.

    Returns:
        The admin token if valid.

    Raises:
        HTTPException: 401 if token is missing or invalid.
    """
    # DANGER: this env var disables all admin authentication.
    # It exists solely for the automated test suite and must NEVER be set in
    # any real deployment — doing so leaves the admin panel completely open.
    if os.environ.get("AUBERGE_DISABLE_ADMIN_AUTH", "").strip() == "1":
        return "test-bypass"

    config = get_config()
    secret = config.app.admin_jwt_secret.strip()
    if not secret:
        raise HTTPException(status_code=500, detail="Admin JWT secret is not configured")

    if not x_admin_token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        verify_admin_jwt(x_admin_token, secret)
    except ValueError:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return x_admin_token


def is_admin_request(x_admin_token: str = Header(default="")) -> bool:
    """Non-raising variant of :func:`get_admin_token`.

    Used by endpoints that stay public but return more data to an authenticated
    admin (e.g. the character list when public listing is disabled).
    """
    try:
        get_admin_token(x_admin_token)
    except HTTPException:
        return False
    return True


@router.post(
    "/login",
    response_model=AdminLoginResponse,
    responses={429: {"description": "Too many login attempts"}},
)
def admin_login(request: AdminLoginRequest, http_request: Request) -> AdminLoginResponse:
    """Authenticate to admin panel with password.

    Args:
        request: Contains the plain-text password.
        http_request: Used to identify the client IP for rate limiting.

    Returns:
        AdminLoginResponse with an auth token.

    Raises:
        HTTPException: 401 if password is incorrect, or 429 if too many recent
            failed attempts came from the same IP.
    """
    config = get_config()
    client_ip = _get_admin_login_client_ip(http_request)

    if not config.app.admin_password_hash:
        raise HTTPException(status_code=403, detail="Admin authentication not configured")

    _check_admin_login_rate_limit(client_ip)

    if not verify_password(request.password, config.app.admin_password_hash):
        _record_admin_login_failure(client_ip)
        raise HTTPException(status_code=401, detail="Invalid password")

    _reset_admin_login_failures(client_ip)

    secret = config.app.admin_jwt_secret.strip()
    if not secret:
        raise HTTPException(status_code=500, detail="Admin JWT secret is not configured")
    token = create_admin_jwt(secret, config.app.admin_token_ttl_seconds)

    return AdminLoginResponse(token=token)


@router.post("/logout")
def admin_logout(token: str = Depends(get_admin_token)) -> dict[str, str]:
    """Logout from admin panel.

    Args:
        token: The admin token (validated by dependency).

    Returns:
        Success message.

    Note:
        JWT auth is stateless, so server-side logout cannot invalidate existing
        tokens. This endpoint is kept for backward compatibility; clients
        should delete their stored token.
    """
    # token parameter kept for API compatibility but unused in stateless JWT auth.
    return {"message": "Logged out"}
