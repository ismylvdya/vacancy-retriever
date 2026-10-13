from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .app_info import DISPLAY_TIMEZONE

LOCAL_TZ = ZoneInfo(DISPLAY_TIMEZONE)


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def local(value: datetime | str | None) -> datetime | None:
    if isinstance(value, str):
        value = from_iso(value)
    return value.astimezone(LOCAL_TZ) if value else None


def format_local(value: datetime | str | None, fmt: str = "%d %b %H:%M") -> str:
    converted = local(value)
    return converted.strftime(fmt) if converted else ""
