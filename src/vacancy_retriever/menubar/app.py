from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import Any

from ..app_info import APP_NAME
from ..config import AppConfig, ConfigError
from ..paths import PATHS
from .browser import open_feed

POLL_SECONDS = 5
APP_BUNDLE = Path.home() / "Applications" / f"{APP_NAME}.app"
# Template image: one colour on a transparent background; macOS recolours it for light / dark menu bars.
ICON = Path(__file__).with_name("icon.svg")
ICON_HEIGHT = 17  # points; rumps defaults to 20×20, taller than the system icons


def fetch_status(port: int) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def status_text(status: dict[str, Any] | None) -> str:
    """Status line in the menu; the menu bar itself shows only the icon."""
    if status is None:
        return "Server is not running — open Vacancy Retriever again"
    if status.get("running"):
        return f"Run in progress: {round(status.get('percent') or 0)}%"
    feed = status.get("feed_count")
    line = status.get("last_run_line") or "No runs yet"
    return f"{line} · {feed} in feed" if feed else line


def stop_server() -> None:
    pid_file = PATHS.data / "server.pid"
    try:
        os.kill(int(pid_file.read_text().strip()), signal.SIGTERM)
    except (OSError, ValueError):
        pass


def _osascript(script: str) -> str:
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "osascript failed")
    return result.stdout.strip()


def launch_at_login_enabled() -> bool:
    """The same list as System Settings → General → Login Items (via System Events, nothing written to ~/Library)."""
    try:
        names = _osascript('tell application "System Events" to get the name of every login item')
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return False
    return APP_NAME in [name.strip() for name in names.split(",")]


def set_launch_at_login(enabled: bool) -> None:
    if enabled:
        _osascript('tell application "System Events" to make login item at end with properties '
                   f'{{path:"{APP_BUNDLE}", hidden:true}}')
    else:
        _osascript(f'tell application "System Events" to delete (every login item whose name is "{APP_NAME}")')


def run_menubar(port: int) -> None:
    import rumps
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    url = f"http://127.0.0.1:{port}/"

    class MenuBarApp(rumps.App):
        def __init__(self) -> None:
            super().__init__(APP_NAME, icon=str(ICON), template=True, quit_button=None)
            self.status_item = rumps.MenuItem("…")
            self.login_item = rumps.MenuItem("Launch at Login", callback=self.toggle_login)
            self.login_item.state = launch_at_login_enabled()
            self.menu = [rumps.MenuItem(f"Open {APP_NAME}", callback=self.open_feed), self.status_item, None,
                         self.login_item, None, rumps.MenuItem(f"Quit {APP_NAME}", callback=self.quit_all, key="q")]

        def toggle_login(self, sender: Any) -> None:
            if not APP_BUNDLE.exists():
                rumps.alert(APP_NAME, f"{APP_BUNDLE} not found. Build it first: scripts/build-app.sh")
                return
            try:
                set_launch_at_login(not sender.state)
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                rumps.alert(APP_NAME, f"Could not change login items: {exc}\n\nAllow control of System Events in "
                                      "System Settings → Privacy & Security → Automation.")
            sender.state = launch_at_login_enabled()

        def quit_all(self, _sender: Any = None) -> None:
            """Stops the server too (a run in progress is rolled back on the next start)."""
            stop_server()
            rumps.quit_application()

        def open_feed(self, _sender: Any = None) -> None:
            try:
                browser = AppConfig().ui.browser  # re-read: config/app.yaml edits apply without a restart
            except ConfigError as exc:
                rumps.alert(APP_NAME, str(exc))
                return
            threading.Thread(target=open_feed, args=(url, browser), daemon=True).start()  # keep the menu responsive

        @rumps.timer(POLL_SECONDS)
        def refresh(self, _timer: Any) -> None:
            self.status_item.title = status_text(fetch_status(port))

    menubar = MenuBarApp()

    @rumps.events.before_start.register
    def left_click_opens_feed() -> None:
        """Left click opens the feed; right click (or Ctrl+click) shows the menu — like most menu bar utilities."""
        from AppKit import NSEvent, NSEventMaskLeftMouseUp, NSEventMaskRightMouseUp, NSEventModifierFlagControl
        from AppKit import NSEventTypeRightMouseUp
        from Foundation import NSObject

        status_item = menubar._nsapp.nsstatusitem
        menu = status_item.menu()
        image = status_item.button().image()
        if image is not None:
            image.setSize_((ICON_HEIGHT * 112 / 110, ICON_HEIGHT))  # icon.svg is 112×110

        class ClickHandler(NSObject):
            def clicked_(self, _sender: Any) -> None:
                event = NSApplication.sharedApplication().currentEvent()
                right = event is not None and (
                    event.type() == NSEventTypeRightMouseUp
                    or bool(NSEvent.modifierFlags() & NSEventModifierFlagControl))
                if right:
                    status_item.setMenu_(menu)
                    status_item.button().performClick_(None)
                    status_item.setMenu_(None)
                else:
                    menubar.open_feed()

        handler = ClickHandler.alloc().init()
        menubar._click_handler = handler  # keep a reference: the button holds its target weakly
        status_item.setMenu_(None)
        button = status_item.button()
        button.setTarget_(handler)
        button.setAction_("clicked:")
        button.sendActionOn_(NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp)

    NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    menubar.run()
