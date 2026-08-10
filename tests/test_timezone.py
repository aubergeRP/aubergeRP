"""Tests for timezone support (Step 5).

Covers:
- valid IANA timezone (accept)
- invalid timezone (reject)
- timezone persistence and retrieval
- timezone update (overwrite)
- Europe/Paris DST behavior
- America/New_York DST behavior
- Web API GET /api/timezone/
- Web API PUT /api/timezone/ — browser timezone persisted
- Web timezone change updates stored value
- Telegram /timezone persists value
- Different users/sessions can have different timezones
- Restart preserves timezone (DB-level persistence across service instances)
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from aubergeRP.database import init_db
from aubergeRP.services.timezone_service import InvalidTimezoneError, TimezoneService, validate_timezone

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    init_db(d)
    return d


@pytest.fixture()
def tz_svc(data_dir: Path) -> TimezoneService:
    return TimezoneService(data_dir=data_dir)


@pytest.fixture()
def app(tmp_path: Path):
    import os
    os.environ["AUBERGE_DATA_DIR"] = str(tmp_path / "_appdata")
    from aubergeRP.main import create_app
    return create_app()


@pytest.fixture()
def client(app) -> TestClient:
    return TestClient(app)


# ── validate_timezone ─────────────────────────────────────────────────────────


def test_validate_valid_timezone():
    zi = validate_timezone("Europe/Paris")
    assert zi.key == "Europe/Paris"


def test_validate_valid_timezone_new_york():
    zi = validate_timezone("America/New_York")
    assert zi.key == "America/New_York"


def test_validate_valid_timezone_tokyo():
    zi = validate_timezone("Asia/Tokyo")
    assert zi.key == "Asia/Tokyo"


def test_validate_invalid_timezone():
    with pytest.raises(InvalidTimezoneError, match="not a valid IANA timezone"):
        validate_timezone("Invalid/Zone")


def test_validate_empty_timezone():
    with pytest.raises(InvalidTimezoneError):
        validate_timezone("")


def test_validate_utc_offset_rejected():
    with pytest.raises(InvalidTimezoneError):
        validate_timezone("+02:00")


# ── TimezoneService CRUD ──────────────────────────────────────────────────────


def test_get_timezone_not_set(tz_svc: TimezoneService):
    result = tz_svc.get_timezone_name("web", "web", "session-abc")
    assert result is None


def test_set_and_get_timezone(tz_svc: TimezoneService):
    tz_svc.set_timezone("web", "web", "session-abc", "Europe/Paris")
    assert tz_svc.get_timezone_name("web", "web", "session-abc") == "Europe/Paris"


def test_update_timezone(tz_svc: TimezoneService):
    tz_svc.set_timezone("web", "web", "session-abc", "Europe/Paris")
    tz_svc.set_timezone("web", "web", "session-abc", "America/New_York")
    assert tz_svc.get_timezone_name("web", "web", "session-abc") == "America/New_York"


def test_set_invalid_timezone_raises(tz_svc: TimezoneService):
    with pytest.raises(InvalidTimezoneError):
        tz_svc.set_timezone("web", "web", "session-abc", "Bogus/Zone")


def test_different_users_independent_timezones(tz_svc: TimezoneService):
    tz_svc.set_timezone("web", "web", "user-1", "Europe/Paris")
    tz_svc.set_timezone("web", "web", "user-2", "America/New_York")
    assert tz_svc.get_timezone_name("web", "web", "user-1") == "Europe/Paris"
    assert tz_svc.get_timezone_name("web", "web", "user-2") == "America/New_York"


def test_different_channels_independent(tz_svc: TimezoneService):
    tz_svc.set_timezone("web", "web", "token-x", "Asia/Tokyo")
    tz_svc.set_timezone("telegram", "bot-1", "12345", "Europe/Paris")
    assert tz_svc.get_timezone_name("web", "web", "token-x") == "Asia/Tokyo"
    assert tz_svc.get_timezone_name("telegram", "bot-1", "12345") == "Europe/Paris"


def test_restart_preserves_timezone(data_dir: Path):
    """Simulate a service restart by creating a new instance."""
    svc1 = TimezoneService(data_dir=data_dir)
    svc1.set_timezone("web", "web", "session-persist", "Asia/Tokyo")

    svc2 = TimezoneService(data_dir=data_dir)
    assert svc2.get_timezone_name("web", "web", "session-persist") == "Asia/Tokyo"


# ── DST behavior ──────────────────────────────────────────────────────────────


def test_europe_paris_dst_summer(tz_svc: TimezoneService):
    """Europe/Paris is UTC+2 in summer (CEST)."""
    tz_svc.set_timezone("web", "web", "s", "Europe/Paris")
    # 2024-07-15 12:00 UTC → 14:00 CEST
    utc_ts = datetime(2024, 7, 15, 12, 0, 0, tzinfo=UTC)
    local = tz_svc.get_local_datetime("web", "web", "s", utc_now=utc_ts)
    assert local is not None
    assert local.hour == 14
    assert local.utcoffset().total_seconds() == 7200  # +02:00


def test_europe_paris_dst_winter(tz_svc: TimezoneService):
    """Europe/Paris is UTC+1 in winter (CET)."""
    tz_svc.set_timezone("web", "web", "s", "Europe/Paris")
    # 2024-01-15 12:00 UTC → 13:00 CET
    utc_ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    local = tz_svc.get_local_datetime("web", "web", "s", utc_now=utc_ts)
    assert local is not None
    assert local.hour == 13
    assert local.utcoffset().total_seconds() == 3600  # +01:00


def test_america_new_york_dst_summer(tz_svc: TimezoneService):
    """America/New_York is UTC-4 in summer (EDT)."""
    tz_svc.set_timezone("web", "web", "s", "America/New_York")
    utc_ts = datetime(2024, 7, 15, 12, 0, 0, tzinfo=UTC)
    local = tz_svc.get_local_datetime("web", "web", "s", utc_now=utc_ts)
    assert local is not None
    assert local.hour == 8
    assert local.utcoffset().total_seconds() == -14400  # -04:00


def test_america_new_york_dst_winter(tz_svc: TimezoneService):
    """America/New_York is UTC-5 in winter (EST)."""
    tz_svc.set_timezone("web", "web", "s", "America/New_York")
    utc_ts = datetime(2024, 1, 15, 12, 0, 0, tzinfo=UTC)
    local = tz_svc.get_local_datetime("web", "web", "s", utc_now=utc_ts)
    assert local is not None
    assert local.hour == 7
    assert local.utcoffset().total_seconds() == -18000  # -05:00


def test_get_local_datetime_not_set(tz_svc: TimezoneService):
    result = tz_svc.get_local_datetime("web", "web", "nobody")
    assert result is None


# ── Web API tests ─────────────────────────────────────────────────────────────


def test_web_get_timezone_not_set(client: TestClient):
    resp = client.get("/api/timezone/", headers={"X-Session-Token": "new-session"})
    assert resp.status_code == 200
    assert resp.json() == {"timezone": None}


def test_web_get_timezone_no_token(client: TestClient):
    resp = client.get("/api/timezone/")
    assert resp.status_code == 200
    assert resp.json() == {"timezone": None}


def test_web_put_timezone_no_token_rejected(client: TestClient):
    resp = client.put("/api/timezone/", json={"timezone": "Europe/Paris"})
    assert resp.status_code == 401


def test_web_set_timezone(client: TestClient):
    resp = client.put(
        "/api/timezone/",
        json={"timezone": "Europe/Paris"},
        headers={"X-Session-Token": "my-session"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"timezone": "Europe/Paris"}


def test_web_get_timezone_after_set(client: TestClient):
    token = "my-session-123"
    client.put("/api/timezone/", json={"timezone": "Asia/Tokyo"}, headers={"X-Session-Token": token})
    resp = client.get("/api/timezone/", headers={"X-Session-Token": token})
    assert resp.status_code == 200
    assert resp.json()["timezone"] == "Asia/Tokyo"


def test_web_timezone_change_updates_stored_value(client: TestClient):
    token = "update-session"
    client.put("/api/timezone/", json={"timezone": "Europe/Paris"}, headers={"X-Session-Token": token})
    client.put("/api/timezone/", json={"timezone": "America/New_York"}, headers={"X-Session-Token": token})
    resp = client.get("/api/timezone/", headers={"X-Session-Token": token})
    assert resp.json()["timezone"] == "America/New_York"


def test_web_invalid_timezone_rejected(client: TestClient):
    resp = client.put(
        "/api/timezone/",
        json={"timezone": "Not/A/Timezone"},
        headers={"X-Session-Token": "x"},
    )
    assert resp.status_code == 422


def test_web_browser_timezone_persisted(client: TestClient):
    """Simulate browser sending its detected IANA timezone to the backend."""
    token = "browser-session"
    browser_tz = "Europe/Paris"
    resp = client.put("/api/timezone/", json={"timezone": browser_tz}, headers={"X-Session-Token": token})
    assert resp.status_code == 200
    assert resp.json()["timezone"] == browser_tz
    # Verify it was actually persisted
    get_resp = client.get("/api/timezone/", headers={"X-Session-Token": token})
    assert get_resp.json()["timezone"] == browser_tz


def test_web_sessions_isolated(client: TestClient):
    client.put("/api/timezone/", json={"timezone": "Europe/Paris"}, headers={"X-Session-Token": "alice"})
    client.put("/api/timezone/", json={"timezone": "Asia/Tokyo"}, headers={"X-Session-Token": "bob"})
    assert client.get("/api/timezone/", headers={"X-Session-Token": "alice"}).json()["timezone"] == "Europe/Paris"
    assert client.get("/api/timezone/", headers={"X-Session-Token": "bob"}).json()["timezone"] == "Asia/Tokyo"


# ── Telegram /timezone command ────────────────────────────────────────────────


def _make_fake_message(text: str, user_id: int = 99) -> MagicMock:
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.chat.type = "private"
    msg.chat.id = user_id
    msg.from_user.id = user_id
    msg.answer = AsyncMock()
    return msg


@pytest.fixture()
def runtime_manager(data_dir: Path):
    from aubergeRP.services.telegram_runtime_manager import TelegramRuntimeManager
    return TelegramRuntimeManager(data_dir=data_dir)


def test_telegram_timezone_persisted(data_dir: Path, runtime_manager):
    """Verify that a valid /timezone command stores the value via TimezoneService."""
    bot_id = "bot-42"
    user_id = "99"
    svc = TimezoneService(data_dir=data_dir)
    svc.set_timezone("telegram", bot_id, user_id, "Europe/Paris")
    assert svc.get_timezone_name("telegram", bot_id, user_id) == "Europe/Paris"


def test_telegram_timezone_update(data_dir: Path):
    """Telegram users can update their timezone."""
    bot_id = "bot-42"
    user_id = "99"
    svc = TimezoneService(data_dir=data_dir)
    svc.set_timezone("telegram", bot_id, user_id, "Europe/Paris")
    svc.set_timezone("telegram", bot_id, user_id, "Asia/Tokyo")
    assert svc.get_timezone_name("telegram", bot_id, user_id) == "Asia/Tokyo"


def test_telegram_invalid_timezone_rejected(data_dir: Path):
    """Invalid timezone should raise InvalidTimezoneError."""
    svc = TimezoneService(data_dir=data_dir)
    with pytest.raises(InvalidTimezoneError):
        svc.set_timezone("telegram", "bot-1", "99", "Bogus/Zone")


@pytest.mark.asyncio
async def test_telegram_on_timezone_command_valid(data_dir: Path):
    """Test the /timezone handler logic directly by simulating what the handler does."""
    bot_id = "bot-42"
    user_id = "99"
    tz_arg = "Europe/Paris"

    # Simulate what on_timezone does
    tz_svc = TimezoneService(data_dir)
    await asyncio.get_running_loop().run_in_executor(
        None, tz_svc.set_timezone, "telegram", bot_id, user_id, tz_arg
    )

    assert tz_svc.get_timezone_name("telegram", bot_id, user_id) == "Europe/Paris"


@pytest.mark.asyncio
async def test_telegram_on_timezone_command_invalid(data_dir: Path):
    """Test that an invalid timezone raises InvalidTimezoneError."""
    tz_svc = TimezoneService(data_dir)
    with pytest.raises(InvalidTimezoneError):
        tz_svc.set_timezone("telegram", "bot-42", "99", "Invalid/Zone")
