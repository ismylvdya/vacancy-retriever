from __future__ import annotations

import json
import urllib.request
import webbrowser
from typing import Any

from ..app_info import APP_NAME, MENUBAR_TITLE

POLL_SECONDS = 5


def fetch_status(port: int) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def status_text(status: dict[str, Any] | None) -> tuple[str, str]:
    """(menu bar title, status line)."""
    if status is None:
        return f"{MENUBAR_TITLE}⚠", "Server is not running — scripts/start.sh"
    if status.get("running"):
        percent = round(status.get("percent") or 0)
        return f"{MENUBAR_TITLE} {percent}%", f"Run in progress: {percent}%"
    feed = status.get("feed_count")
    title = f"{MENUBAR_TITLE} {feed}" if feed else MENUBAR_TITLE
    return title, status.get("last_run_line") or "No runs yet"


def run_menubar(port: int) -> None:
    import rumps
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    url = f"http://127.0.0.1:{port}/"

    class MenuBarApp(rumps.App):
        def __init__(self) -> None:
            super().__init__(APP_NAME, title=MENUBAR_TITLE, quit_button="Quit menu bar icon")
            self.status_item = rumps.MenuItem("…")
            self.menu = [rumps.MenuItem(f"Open {APP_NAME}", callback=self.open_feed), self.status_item]

        def open_feed(self, _sender: Any = None) -> None:
            webbrowser.open(url)

        @rumps.timer(POLL_SECONDS)
        def refresh(self, _timer: Any) -> None:
            title, line = status_text(fetch_status(port))
            self.title = title
            self.status_item.title = line

    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    MenuBarApp().run()
