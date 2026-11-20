from __future__ import annotations

import os
from datetime import UTC, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .app_info import DISPLAY_TIMEZONE

_LOCALTIME = "/etc/localtime"  # macOS: a link to …/zoneinfo/<Area/City> of System Settings → Date & Time


def machine_timezone() -> tzinfo:
    """The Mac's current time zone, read on every call (travel: no restart needed). The database keeps UTC;
    only what is shown uses this. TZ in the environment wins; DISPLAY_TIMEZONE if nothing can be read."""
    name = os.environ.get("TZ", "").lstrip(":")
    if not name:
        target = os.path.realpath(_LOCALTIME)
        name = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""
    try:
        return ZoneInfo(name or DISPLAY_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DISPLAY_TIMEZONE)


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
    return value.astimezone(machine_timezone()) if value else None


def day_start(now: datetime, at: time) -> datetime:
    """The latest moment `at` (local time of this Mac) that is not after `now`: with 03:00, at 02:30 it is yesterday's
    03:00 and at 03:00 today's."""
    current = local(now)
    assert current is not None
    start = current.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return start if start <= current else start - timedelta(days=1)


def format_local(value: datetime | str | None, fmt: str = "%d %b %H:%M") -> str:
    converted = local(value)
    return converted.strftime(fmt) if converted else ""
