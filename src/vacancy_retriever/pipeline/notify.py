from __future__ import annotations

import logging
import subprocess
import sys

from ..app_info import APP_NAME

log = logging.getLogger(__name__)


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(message: str, title: str = APP_NAME) -> None:
    """macOS notification via osascript (reliable without an app bundle)."""
    if sys.platform != "darwin":
        return
    script = f"display notification {_quote(message)} with title {_quote(title)}"
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], check=False, timeout=10, capture_output=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Notification failed: %s", exc)
