from vacancy_retriever.menubar.app import status_text


def test_server_down():
    assert "not running" in status_text(None)


def test_running_shows_percent():
    assert status_text({"running": True, "percent": 42.4}) == "Run in progress: 42%"


def test_idle_shows_feed_count_and_last_run():
    line = status_text({"running": False, "feed_count": 7, "last_run_line": "Last run: 13 Oct 08:12 (14 min)"})
    assert line == "Last run: 13 Oct 08:12 (14 min) · 7 in feed"


def test_menu_bar_icon_is_a_template_svg():
    from vacancy_retriever.menubar.app import ICON

    assert ICON.exists() and ICON.read_text().lstrip().startswith("<svg")


def test_launch_at_login_reads_and_writes_login_items(monkeypatch):
    from vacancy_retriever.menubar import app

    calls = []

    def fake(script):
        calls.append(script)
        return "Ice, Vacancy Retriever, Paste" if "get the name" in script else ""

    monkeypatch.setattr(app, "_osascript", fake)
    assert app.launch_at_login_enabled()
    app.set_launch_at_login(True)
    assert "make login item" in calls[-1] and "Vacancy Retriever.app" in calls[-1] and "hidden:true" in calls[-1]
    app.set_launch_at_login(False)
    assert 'delete (every login item whose name is "Vacancy Retriever")' in calls[-1]
    monkeypatch.setattr(app, "_osascript", lambda script: "Ice, Paste")
    assert not app.launch_at_login_enabled()


def test_browser_shortcut_script_and_config():
    import pytest
    from pydantic import ValidationError

    from vacancy_retriever.config import BrowserSettings
    from vacancy_retriever.menubar.browser import shortcut_script

    settings = BrowserSettings(app="Arc", shortcut_before_open="Ctrl+2")
    assert settings.shortcut_before_open == "ctrl+2"
    script = shortcut_script(settings.shortcut_before_open, app="Arc")
    assert 'tell application "Arc" to activate' in script
    assert 'keystroke "2" using {control down}' in script
    default = shortcut_script("ctrl+2", bundle_id="app.zen-browser.zen")
    assert 'tell application id "app.zen-browser.zen" to activate' in default
    assert BrowserSettings(shortcut_before_open="").shortcut_before_open is None
    with pytest.raises(ValidationError):
        BrowserSettings(shortcut_before_open="hyper+22")


def test_project_app_yaml_is_valid():
    from vacancy_retriever.config import AppConfig

    browser = AppConfig().ui.browser
    assert browser.app is None and browser.shortcut_before_open == "ctrl+2", "default browser + Ctrl+2 (17.11.2026)"


def test_default_browser_is_read_from_launch_services(tmp_path):
    import plistlib

    from vacancy_retriever.menubar.browser import SAFARI, default_browser_id

    plist = tmp_path / "ls.plist"
    plist.write_bytes(plistlib.dumps({"LSHandlers": [
        {"LSHandlerContentType": "public.html", "LSHandlerRoleAll": "com.other"},
        {"LSHandlerURLScheme": "https", "LSHandlerRoleAll": "app.zen-browser.zen"}]}))
    assert default_browser_id(plist) == "app.zen-browser.zen"
    assert default_browser_id(tmp_path / "missing.plist") == SAFARI


def test_feed_opens_in_the_default_browser_after_the_shortcut(monkeypatch):
    from vacancy_retriever.config import BrowserSettings
    from vacancy_retriever.menubar import browser

    calls = []
    monkeypatch.setattr(browser.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(browser.time, "sleep", lambda s: None)
    monkeypatch.setattr(browser, "default_browser_id", lambda: "app.zen-browser.zen")
    browser.open_feed("http://127.0.0.1:18765/", BrowserSettings(shortcut_before_open="ctrl+2"))
    assert calls[0][0] == "osascript" and 'application id "app.zen-browser.zen"' in calls[0][2]
    assert calls[1] == ["open", "http://127.0.0.1:18765/"]
