from __future__ import annotations

import threading

import httpx
import pytest

from vacancy_retriever.collectors.http import ForbiddenDomainError, PoliteHttpClient
from vacancy_retriever.config import AppConfig, SourceConfig, SourcesFile
from vacancy_retriever.pipeline.manager import AlreadyRunning, RunManager
from vacancy_retriever.pipeline.run_lock import RunLock
from vacancy_retriever.pipeline.runner import RunOptions
from vacancy_retriever.storage import Storage
from vacancy_retriever.storage.runs import SourceRunRecord
from vacancy_retriever.web import views


@pytest.fixture
def setup(tmp_paths):
    config = AppConfig(tmp_paths)
    config.__dict__["sources"] = SourcesFile(sources=[SourceConfig(id="chan", type="telegram", name="Chan")])
    storage = Storage.open(tmp_paths.default_db, tmp_paths.token_log)
    yield config, storage, tmp_paths
    storage.db.close()


def test_second_run_is_refused_while_lock_is_held(setup):
    config, storage, paths = setup
    other_process = RunLock(paths.run_lock)
    assert other_process.acquire()
    try:
        manager = RunManager(config, storage)
        assert manager.is_running()
        with pytest.raises(AlreadyRunning):
            manager.start(RunOptions(notify=False))
        with pytest.raises(AlreadyRunning):
            manager.run_foreground(RunOptions(notify=False))
    finally:
        other_process.release()
    assert not RunManager(config, storage).is_running()


def test_recover_does_not_touch_a_run_of_another_process(setup):
    config, storage, paths = setup
    run_id = storage.runs.start({})
    other_process = RunLock(paths.run_lock)
    other_process.acquire()
    try:
        assert RunManager(config, storage).recover() == []
        assert storage.runs.get(run_id)["status"] == "running"
    finally:
        other_process.release()
    assert RunManager(config, storage).recover() == [run_id]


def test_status_reports_idle_with_no_runs(setup):
    config, storage, _ = setup
    status = RunManager(config, storage).status()
    assert status["running"] is False and status["last_run"] is None


def _source_run(storage: Storage, seen: int, collected: int) -> None:
    run_id = storage.runs.start({})
    storage.runs.record_source(run_id, SourceRunRecord("chan", "ok", collected=collected, seen=seen))
    storage.runs.commit(run_id, {}, 30)


def test_zero_alert_only_when_source_showed_nothing(setup):
    config, storage, _ = setup
    _source_run(storage, seen=20, collected=5)
    _source_run(storage, seen=20, collected=0)
    runs = views.health(config, storage)
    assert not runs[0].rows[0].alert, "no new posts but the page was fine — not an alert"
    _source_run(storage, seen=0, collected=0)
    runs = views.health(config, storage)
    assert "showed nothing" in runs[0].rows[0].alert
    assert runs[0].rows[0].warn


def test_query_params_are_merged_with_url_query():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    client = PoliteHttpClient(0, 0, transport=httpx.MockTransport(handler))
    client.get_json("https://api.example.com/v?grade=junior&city=bg", page=2)
    assert seen == ["https://api.example.com/v?grade=junior&city=bg&page=2"]


def test_linkedin_is_never_opened():
    client = PoliteHttpClient(0, 0, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    for url in ("https://www.linkedin.com/jobs/view/1", "https://lnkd.in/abc", "https://linkedin.com/favicon.ico"):
        with pytest.raises(ForbiddenDomainError):
            client.get(url)


@pytest.mark.parametrize(("status", "body", "alert"), [
    (403, "Forbidden", "http_403"),
    (429, "slow down", "http_429"),
    (200, "<html><title>Just a moment...</title>cf-chl</html>", "cloudflare"),
    (200, "<html>Please solve the captcha</html>", "captcha"),
])
def test_blocking_signs_are_detected(status, body, alert):
    from vacancy_retriever.collectors.http import BlockedError

    transport = httpx.MockTransport(lambda r: httpx.Response(status, text=body, headers={"content-type": "text/html"}))
    client = PoliteHttpClient(0, 0, retries=0, transport=transport)
    with pytest.raises(BlockedError) as error:
        client.get("https://site.example/jobs")
    assert error.value.alert == alert


def test_cancel_event_is_fresh_per_run(setup):
    config, storage, _ = setup
    manager = RunManager(config, storage)
    first = manager.cancel_event()
    first.set()
    assert isinstance(first, threading.Event)
    assert manager.cancel() is False
