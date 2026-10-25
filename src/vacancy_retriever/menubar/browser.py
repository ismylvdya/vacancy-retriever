from __future__ import annotations

import subprocess
import time

from ..config import BrowserSettings

MODIFIERS = {"ctrl": "control down", "cmd": "command down", "alt": "option down", "shift": "shift down"}


def shortcut_script(app: str, shortcut: str) -> str:
    """AppleScript: bring the browser forward and press the shortcut (e.g. Arc's Ctrl+2 = second space)."""
    *mods, key = shortcut.split("+")
    using = f" using {{{', '.join(MODIFIERS[m] for m in mods)}}}" if mods else ""
    return (f'tell application "{app}" to activate\n'
            "delay 0.3\n"
            f'tell application "System Events" to keystroke "{key}"{using}')


def open_feed(url: str, browser: BrowserSettings) -> None:
    """Opens the feed; with a shortcut set, presses it in the browser first so the tab lands in that space."""
    if browser.app and browser.shortcut_before_open:
        subprocess.run(["osascript", "-e", shortcut_script(browser.app, browser.shortcut_before_open)],
                       capture_output=True, timeout=15)
        time.sleep(0.3)
    command = ["open", "-a", browser.app, url] if browser.app else ["open", url]
    if subprocess.run(command, capture_output=True, timeout=15).returncode != 0 and browser.app:
        subprocess.run(["open", url], timeout=15)
