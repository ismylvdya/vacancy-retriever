from vacancy_retriever.menubar.app import status_text


def test_server_down():
    title, line = status_text(None)
    assert "⚠" in title and "start.sh" in line


def test_running_shows_percent():
    title, line = status_text({"running": True, "percent": 42.4})
    assert title.endswith("42%") and "42%" in line


def test_idle_shows_feed_count_and_last_run():
    title, line = status_text({"running": False, "feed_count": 7, "last_run_line": "Last run: 13 Oct 08:12 (14 min)"})
    assert title.endswith(" 7") and line.startswith("Last run")
