from __future__ import annotations

import plistlib
import subprocess
import time
from pathlib import Path

from ..config import BrowserSettings

MODIFIERS = {"ctrl": "control down", "cmd": "command down", "alt": "option down", "shift": "shift down"}
# Where macOS keeps the default browser (System Settings → Desktop & Dock → Default web browser); read only.
LAUNCH_SERVICES = Path.home() / "Library/Preferences/com.apple.LaunchServices/com.apple.launchservices.secure.plist"
SAFARI = "com.apple.Safari"  # macOS's default when the user never chose another browser


def default_browser_id(plist: Path = LAUNCH_SERVICES) -> str:
    """Bundle id of the system's default browser (the handler of https://), e.g. app.zen-browser.zen."""
    try:
        with plist.open("rb") as handle:
            handlers = plistlib.load(handle).get("LSHandlers") or []
    except (OSError, plistlib.InvalidFileException):
        return SAFARI
    for scheme in ("https", "http"):
        for handler in handlers:
            if handler.get("LSHandlerURLScheme") == scheme and handler.get("LSHandlerRoleAll"):
                return str(handler["LSHandlerRoleAll"])
    return SAFARI


def shortcut_script(shortcut: str, app: str | None = None, bundle_id: str | None = None) -> str:
    """AppleScript: bring the browser forward and press the shortcut (e.g. Ctrl+2 = the second space)."""
    *mods, key = shortcut.split("+")
    using = f" using {{{', '.join(MODIFIERS[m] for m in mods)}}}" if mods else ""
    target = f'application "{app}"' if app else f'application id "{bundle_id or SAFARI}"'
    return (f"tell {target} to activate\n"
            "delay 0.3\n"
            f'tell application "System Events" to keystroke "{key}"{using}')


def open_feed(url: str, browser: BrowserSettings) -> None:
    """Opens the feed in the browser of app.yaml, or in the system's default one when none is named; with a
    shortcut set, presses it in that browser first so the tab lands in that space."""
    if browser.shortcut_before_open:
        script = shortcut_script(browser.shortcut_before_open, browser.app, None if browser.app else default_browser_id())
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15)
        time.sleep(0.3)
    command = ["open", "-a", browser.app, url] if browser.app else ["open", url]
    if subprocess.run(command, capture_output=True, timeout=15).returncode != 0 and browser.app:
        subprocess.run(["open", url], timeout=15)
