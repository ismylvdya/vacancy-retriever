"""Claude subscription limits (the 5-hour session and the week), as Claude Code reports them with every request
(claude_agent_sdk RateLimitEvent → raw "unifiedWindows": utilization 0–1 and resetsAt). Saved in
data/claude_limits.json whenever the app calls Claude; the footer bars show the latest values (P3, 30.10.2026).
There is no way to read them without a request, so they are as fresh as the last Claude call (Run, ▶, N, or the
refresh button, which makes one tiny call)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

WINDOWS = (("five_hour", "5-hour limit"), ("seven_day", "Weekly · all models"))


def save(path: Path, raw: dict[str, Any]) -> None:
    windows = raw.get("unifiedWindows")
    if not isinstance(windows, dict):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"windows": windows, "observed_at": time.time()}), encoding="utf-8")


def load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
