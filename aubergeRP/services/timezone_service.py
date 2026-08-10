"""TimezoneService — transport-independent IANA timezone storage and resolution.

Timezones are stored per (channel, channel_instance_id, external_user_id):
- Web sessions:      channel="web",      channel_instance_id="web"
- Telegram sessions: channel="telegram", channel_instance_id=<bot_id>

The service validates IANA identifiers via ``zoneinfo`` from the standard
library and persists them in the ``user_timezones`` table (migration 006).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlmodel import Session, and_, select

from ..db_models import UserTimezoneRow


class InvalidTimezoneError(ValueError):
    """Raised when an IANA timezone identifier is not recognised."""


def validate_timezone(timezone: str) -> ZoneInfo:
    """Return a ``ZoneInfo`` for *timezone*, or raise :exc:`InvalidTimezoneError`."""
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, KeyError, ValueError) as exc:
        raise InvalidTimezoneError(
            f"'{timezone}' is not a valid IANA timezone identifier. "
            "Use a name such as 'Europe/Paris', 'America/New_York', or 'Asia/Tokyo'."
        ) from exc


class TimezoneService:
    """Read and write per-user IANA timezones.

    This service is transport-neutral: the same methods are used by the Web
    API and Telegram command handlers.  The caller identifies a user with the
    triple (channel, channel_instance_id, external_user_id).
    """

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)

    def _get_session(self) -> Session:
        from ..database import get_engine
        return Session(get_engine(self._data_dir))

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _find(
        self,
        session: Session,
        channel: str,
        channel_instance_id: str,
        external_user_id: str,
    ) -> UserTimezoneRow | None:
        return session.exec(
            select(UserTimezoneRow).where(
                and_(
                    UserTimezoneRow.channel == channel,
                    UserTimezoneRow.channel_instance_id == channel_instance_id,
                    UserTimezoneRow.external_user_id == external_user_id,
                )
            )
        ).first()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_timezone_name(
        self,
        channel: str,
        channel_instance_id: str,
        external_user_id: str,
    ) -> str | None:
        """Return the stored IANA timezone name, or ``None`` if not set."""
        with self._get_session() as session:
            row = self._find(session, channel, channel_instance_id, external_user_id)
            return row.timezone if row else None

    def get_local_datetime(
        self,
        channel: str,
        channel_instance_id: str,
        external_user_id: str,
        *,
        utc_now: datetime | None = None,
    ) -> datetime | None:
        """Return *utc_now* converted to the user's local timezone.

        Returns ``None`` if no timezone has been stored for this user.
        DST is handled automatically by ``zoneinfo``.

        Args:
            utc_now: Reference UTC time; defaults to ``datetime.now(UTC)``.
        """
        tz_name = self.get_timezone_name(channel, channel_instance_id, external_user_id)
        if tz_name is None:
            return None
        zi = validate_timezone(tz_name)
        now = utc_now if utc_now is not None else datetime.now(UTC)
        return now.astimezone(zi)

    def set_timezone(
        self,
        channel: str,
        channel_instance_id: str,
        external_user_id: str,
        timezone: str,
    ) -> str:
        """Validate and persist *timezone*.  Returns the stored timezone name.

        Raises :exc:`InvalidTimezoneError` for unknown timezone identifiers.
        """
        validate_timezone(timezone)  # raises InvalidTimezoneError if invalid
        now = datetime.now(UTC)

        with self._get_session() as session:
            row = self._find(session, channel, channel_instance_id, external_user_id)
            if row is None:
                row = UserTimezoneRow(
                    id=str(uuid.uuid4()),
                    channel=channel,
                    channel_instance_id=channel_instance_id,
                    external_user_id=external_user_id,
                    timezone=timezone,
                    updated_at=now,
                )
            else:
                row.timezone = timezone
                row.updated_at = now
            session.add(row)
            session.commit()

        return timezone
