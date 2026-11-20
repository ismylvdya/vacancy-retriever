"""Statuses of applications and internship waves from e-mails (decision of the user 20.11.2026, docs/STATUSES.md)."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any, ClassVar

import pytest

from vacancy_retriever.collectors.base import CollectContext, Collector, CollectResult, SourceCursor
from vacancy_retriever.collectors.email.collector import EmailCollector
from vacancy_retriever.collectors.email.triage import job_related, triage_item
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.registry import register
from vacancy_retriever.config import AppConfig, SourceConfig, SourcesFile
from vacancy_retriever.llm.base import LlmProvider, LlmResponse, LlmUsage
from vacancy_retriever.pipeline.progress import ProgressTracker
from vacancy_retriever.pipeline.runner import Pipeline, RunOptions, source_wanted
from vacancy_retriever.storage import Storage

from .conftest import NOW
from .test_email import FakeImap, make_letter


def letter(sender: str, subject: str, body: str, uid: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = subject
    message["Message-ID"] = f"<m{uid}@example.com>"
    message["Date"] = "Mon, 12 Oct 2026 09:00:00 +0200"
    message.set_content(body)
    return message


def test_job_words():
    assert job_related("Анна <anna@company.ru>", "Приглашение на собеседование")
    assert job_related("noreply@hh.ru", "Ваше резюме просмотрели")
    assert job_related("someone@x.com", "Hello", "Unfortunately we decided to move forward with other candidates")
    assert job_related("talent@acme.com", "Hi")
    assert not job_related("Магазин <shop@store.ru>", "Скидка 20% на кроссовки", "Только сегодня")


# ---------------------------------------------------------------- the collector

@pytest.fixture
def mailbox(tmp_paths, monkeypatch):
    config = AppConfig(tmp_paths)
    monkeypatch.setattr("vacancy_retriever.collectors.email.collector.AppConfig", lambda: config)
    config.__dict__["secrets"] = type(config.secrets)({"GMAIL_LOGIN": "me", "GMAIL_APP_PASSWORD": "pw"})
    letters = {
        "1": make_letter(uid_hint="1").as_bytes(),  # a LinkedIn job alert: a vacancy
        "2": letter("Магазин <shop@store.ru>", "Скидка на кроссовки", "Только сегодня", "2").as_bytes(),
        "3": letter("Анна <anna@company.ru>", "Re: Data Analyst", "Приглашаем вас на собеседование в четверг", "3").as_bytes(),
        "4": letter("hh.ru <noreply@hh.ru>", "Приглашение на интервью", "Компания Х приглашает вас", "4").as_bytes(),
    }
    imap = FakeImap("h", 993, letters)
    monkeypatch.setattr(EmailCollector, "imap_factory", staticmethod(lambda h, p: imap))
    source = SourceConfig(id="gmail", type="email", name="Gmail", params={"mailbox": "gmail"})
    return EmailCollector(source, PoliteHttpClient(0, 0)), imap


def context(remembered: list[str], checked: set[str] | None = None, vacancies: bool = True) -> CollectContext:
    return CollectContext(SourceCursor(), datetime.now(UTC) - timedelta(days=30), 1, lambda _id: False,
                          threading.Event(), mail_checked=lambda: checked or set(),
                          remember_checked=remembered.extend, vacancies_wanted=vacancies)


def test_collector_sends_letters_about_work_to_triage_and_leaves_the_rest(mailbox):
    collector, imap = mailbox
    remembered: list[str] = []
    items = collector.collect(context(remembered)).items
    by_uid = {i.meta["email"]["uid"]: i for i in items}
    assert set(by_uid) == {"1", "3", "4"}
    assert not by_uid["1"].meta.get("triage"), "a job alert of a known template is a vacancy as before"
    assert by_uid["3"].meta["triage"] and by_uid["3"].external_id == "mail:<m3@example.com>"
    assert by_uid["4"].meta["triage"] and by_uid["4"].meta["email"]["parser"] == "hh_ru"
    assert remembered == ["<m2@example.com>"], "the shop letter is remembered, not stored"
    assert all(c[0] not in ("STORE", "MOVE", "COPY") for c in imap.commands), "nothing is marked while collecting"
    # next run: the remembered letter is not even fetched
    fetches = sum(1 for c in imap.commands if c[0] == "FETCH" and c[1] == "2")
    collector.collect(context([], checked={"<m2@example.com>"}))
    assert sum(1 for c in imap.commands if c[0] == "FETCH" and c[1] == "2") == fetches


def test_a_mailbox_whose_groups_are_off_still_reads_statuses(mailbox):
    collector, _ = mailbox
    items = collector.collect(context([], vacancies=False)).items
    assert {i.meta["email"]["uid"] for i in items} == {"3", "4"}, "the job alert stays unread"
    source = SourceConfig(id="gmail", type="email", name="Gmail", groups=[1])
    assert source_wanted(source, {6, 7}), "a mailbox is always collected"
    assert not source_wanted(SourceConfig(id="t", type="telegram", name="T", groups=[1]), {6, 7})


# ---------------------------------------------------------------- the run

MAILS: list[Any] = []
FLAGGED: list[dict[str, Any]] = []


@register
class FakeMailCollector(Collector):
    type_name = "fakemail"
    letters: ClassVar[list[EmailMessage]] = []

    def collect(self, ctx: CollectContext) -> CollectResult:
        items = [triage_item(self.source.id, "gmail", str(i), m, "", "triage" if "@acme" in m["From"] else "hh_ru")
                 for i, m in enumerate(FakeMailCollector.letters, 1)]
        return CollectResult(items=[i for i in items if not ctx.is_known(i.external_id)])

    def after_commit(self, processed_meta):
        FLAGGED.extend(processed_meta)
        return []


class MailProvider(LlmProvider):
    """The mail stage gets answers by subject; scoring gets one wave programme."""

    name = "fake"

    def __init__(self, answers: dict[str, dict]):
        self.answers = answers
        self.stages: list[str] = []

    async def complete_json(self, system, user, model, schema=None) -> LlmResponse:
        payload = json.loads(user)
        if system.startswith("You sort unread e-mails"):
            self.stages.append("mail")
            results = [{"input_id": e["input_id"], **self.answers[e["subject"]]} for e in payload]
        else:
            self.stages.append("scoring")
            assert all(e["hints"]["letter_kind"] == "wave" for e in payload)
            results = [{"input_id": e["input_id"], "vacancies": [{
                "title_original": "Стажировка в Яндексе: аналитика данных", "role_category": "data_analyst",
                "seniority": "intern", "seniority_source": "title", "stack_overlap": "strong", "country": "Russia",
                "city": "Москва", "work_format": "hybrid", "employer_name": "Яндекс", "employer_type": "russian",
                "confidence": "high", "ad_language": "ru", "summary_ru": "Набор до 1 декабря"}]} for e in payload]
        data = {"results": results}
        return LlmResponse(data, LlmUsage(10, 5, 0, 0, model), json.dumps(data), "none")


@pytest.fixture
def env(tmp_paths):
    config = AppConfig(tmp_paths)
    config.__dict__["sources"] = SourcesFile(sources=[SourceConfig(id="gmail", type="fakemail", name="Gmail")])
    storage = Storage.open(tmp_paths.default_db, tmp_paths.token_log)
    FLAGGED.clear()
    FakeMailCollector.letters = [
        letter("hh.ru <noreply@hh.ru>", "Отказ", "К сожалению, мы не готовы пригласить вас", "1"),
        letter("hh.ru <noreply@hh.ru>", "Тестовое", "Пройдите тестовое задание", "2"),
        letter("hh.ru <noreply@hh.ru>", "Волна", "Открыт набор на стажировку", "3"),
        letter("Bob <bob@acme.com>", "Hi", "Job talk over coffee?", "4"),
        letter("Bob <bob@acme.com>", "Maybe", "Your candidacy, maybe", "5"),
    ]
    yield config, storage, tmp_paths
    storage.db.close()


ANSWERS = {
    "Отказ": {"kind": "status", "confidence": "high", "stage": "rejected", "company": "Acme", "position": "Data Analyst",
              "summary_ru": "Отказ"},
    "Тестовое": {"kind": "status", "confidence": "medium", "stage": "test", "company": "Beta", "summary_ru": "Тест"},
    "Волна": {"kind": "wave", "confidence": "high"},
    "Hi": {"kind": "not_job", "confidence": "high"},
    "Maybe": {"kind": "spam", "confidence": "low", "summary_ru": "Похоже на спам"},
}


def run(config, storage, paths, provider):
    pipeline = Pipeline(config, storage, lambda: provider, ProgressTracker(paths.data / "progress.json"),
                        threading.Event(), http_factory=lambda: PoliteHttpClient(0, 0), now=lambda: NOW)
    return pipeline.run(RunOptions(notify=False))


def test_statuses_waves_and_untouched_letters(env):
    config, storage, paths = env
    provider = MailProvider(ANSWERS)
    summary = run(config, storage, paths, provider)
    assert summary.status == "committed" and provider.stages == ["mail", "scoring"]
    statuses = {s.subject: s for s in storage.statuses.visible()}
    assert set(statuses) == {"Отказ", "Тестовое", "Maybe"}
    assert (statuses["Отказ"].stage, statuses["Отказ"].company, statuses["Отказ"].unsure) == ("rejected", "Acme", False)
    assert statuses["Тестовое"].unsure, "a status the LLM is not sure of is shown with a question mark"
    assert (statuses["Maybe"].stage, statuses["Maybe"].unsure) == ("unclear", True), "maybe-spam is shown too"
    assert storage.statuses.new_count() == 3
    [wave] = storage.vacancies.feed()
    assert wave.facts.wave and wave.title.startswith("Стажировка") and wave.group_no == 7
    flagged = {m["email"]["subject"] for m in FLAGGED}
    assert flagged == {"Отказ", "Тестовое", "Волна", "Maybe"}, "read + labelled; «Hi» (not about work) untouched"
    # a second run collects nothing again
    assert run(config, storage, paths, MailProvider({})).status == "committed"
    assert len(storage.statuses.visible()) == 3


def test_statuses_appear_only_with_the_commit(env):
    config, storage, paths = env
    pipeline = Pipeline(config, storage, lambda: MailProvider(ANSWERS), ProgressTracker(paths.data / "progress.json"),
                        threading.Event(), http_factory=lambda: PoliteHttpClient(0, 0), now=lambda: NOW)
    pipeline._dedup = lambda run_id: (_ for _ in ()).throw(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        pipeline.run(RunOptions(notify=False))
    assert storage.db.query_one("SELECT COUNT(*) AS n FROM mail_statuses")["n"] == 0, "rolled back with the run"
    assert FLAGGED == [], "no letter is marked read by a failed run"


def test_statuses_tab_marks_rows_seen(env):
    from fastapi.testclient import TestClient

    from vacancy_retriever.web.app import create_app

    config, storage, paths = env
    run(config, storage, paths, MailProvider(ANSWERS))
    client = TestClient(create_app(config, storage, 18765), base_url="http://127.0.0.1:18765")
    page = client.get("/statuses").text
    assert "Rejected" in page and "Test task" in page and "Unclear" in page and "is-new" in page
    assert 'id="statuses-count">3<' in page
    ids = [s.id for s in storage.statuses.visible()]
    response = client.post("/api/statuses/seen", json={"ids": ids}, headers={"Origin": "http://127.0.0.1:18765"})
    assert response.json() == {"marked": 3, "new": 0}
    page = client.get("/statuses").text
    assert "is-new" not in page and 'id="statuses-count"' not in page and ">Seen<" in page
    wave_card = client.get("/").text
    assert "Internship wave" in wave_card
