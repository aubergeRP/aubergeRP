"""Timezone API router.

Endpoints
---------
GET  /api/timezone/      — return the current session's stored timezone (or null)
PUT  /api/timezone/      — set/update the current session's timezone

Both endpoints identify the caller via the X-Session-Token header, the same
mechanism used by the rest of the Web API.  For Telegram users the timezone
is managed through the /timezone bot command instead.
"""
from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ..services.timezone_service import InvalidTimezoneError, TimezoneService

router = APIRouter(prefix="/timezone", tags=["timezone"])

_WEB_CHANNEL = "web"
_WEB_INSTANCE = "web"


def _get_service() -> TimezoneService:
    from ..config import get_config
    return TimezoneService(data_dir=get_config().app.data_dir)


class TimezoneResponse(BaseModel):
    timezone: str | None


class TimezoneUpdate(BaseModel):
    timezone: str


@router.get("/", response_model=TimezoneResponse)
def get_timezone(
    x_session_token: str = Header(default=""),
) -> TimezoneResponse:
    """Return the IANA timezone stored for this web session, or null."""
    if not x_session_token:
        return TimezoneResponse(timezone=None)
    svc = _get_service()
    tz = svc.get_timezone_name(_WEB_CHANNEL, _WEB_INSTANCE, x_session_token)
    return TimezoneResponse(timezone=tz)


@router.put(
    "/",
    response_model=TimezoneResponse,
    responses={401: {"description": "Missing session token"}},
    openapi_extra={
        "parameters": [
            {
                "name": "x-session-token",
                "in": "header",
                "required": True,
                "schema": {"type": "string"},
            }
        ]
    },
)
def set_timezone(
    body: TimezoneUpdate,
    x_session_token: str = Header(default="", include_in_schema=False),
) -> TimezoneResponse:
    """Validate and persist an IANA timezone for this web session."""
    if not x_session_token:
        raise HTTPException(status_code=401, detail="X-Session-Token header is required")
    svc = _get_service()
    try:
        tz = svc.set_timezone(_WEB_CHANNEL, _WEB_INSTANCE, x_session_token, body.timezone)
    except InvalidTimezoneError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return TimezoneResponse(timezone=tz)
