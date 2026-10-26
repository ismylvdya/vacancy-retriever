"""Developer notices shown as a banner in the UI ("tell Claude about …"), stored in data/notices.json.

Used for one-off events a parser cannot handle well without a real sample — e.g. the first hh.ru saved-search
letter. Each notice is added once (by id) and stays until dismissed in the UI."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .timeutil import to_iso, utcnow

_LOCK = threading.Lock()


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def add_once(path: Path, notice_id: str, title: str, text: str) -> bool:
    """False when a notice with this id already exists (shown or dismissed)."""
    with _LOCK:
        notices = _read(path)
        if notice_id in notices:
            return False
        notices[notice_id] = {"title": title, "text": text, "created": to_iso(utcnow()), "dismissed": False}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(notices, ensure_ascii=False, indent=2), encoding="utf-8")
        return True


def active(path: Path) -> list[dict[str, Any]]:
    return [{"id": key, **value} for key, value in _read(path).items() if not value.get("dismissed")]


def dismiss(path: Path, notice_id: str) -> bool:
    with _LOCK:
        notices = _read(path)
        if notice_id not in notices:
            return False
        notices[notice_id]["dismissed"] = True
        path.write_text(json.dumps(notices, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
