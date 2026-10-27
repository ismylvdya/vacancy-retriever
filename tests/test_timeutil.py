"""Shown times follow the Mac's time zone; stored times are UTC."""

from datetime import UTC, datetime

from vacancy_retriever.timeutil import format_local, machine_timezone, to_iso


def test_display_follows_the_machine_time_zone(monkeypatch):
    moment = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
    monkeypatch.setenv("TZ", "Asia/Bangkok")
    assert format_local(moment, "%H:%M") == "19:00"
    monkeypatch.setenv("TZ", "Europe/Belgrade")
    assert format_local(moment, "%H:%M") == "13:00"
    monkeypatch.setenv("TZ", "Not/AZone")
    assert str(machine_timezone()) == "Europe/Belgrade"  # fallback
    assert to_iso(moment) == "2026-10-28T12:00:00+00:00"  # storage stays UTC
