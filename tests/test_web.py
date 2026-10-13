from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from vacancy_retriever.collectors.base import RawItem
from vacancy_retriever.config import AppConfig
from vacancy_retriever.scoring.engine import ScoringEngine
from vacancy_retriever.scoring.facts import VacancyFacts
from vacancy_retriever.storage import Storage
from vacancy_retriever.storage.llm_calls import LlmCallRecord
from vacancy_retriever.storage.vacancies import NewVacancy
from vacancy_retriever.timeutil import utcnow
from vacancy_retriever.web.app import create_app

PORT = 18765
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}


@pytest.fixture
def client(tmp_paths):
    config = AppConfig(tmp_paths)
    storage = Storage.open(tmp_paths.default_db, tmp_paths.token_log)
    run_id = storage.runs.start({})
    item = RawItem("serbiawork", "1", "https://telegram.me/serbiawork/1", "Полный текст вакансии про Data Science",
                   utcnow() - timedelta(hours=3), multi=True)
    raw_id = storage.items.insert(run_id, item, "scored")
    facts = VacancyFacts.model_validate({
        "title_original": "Junior Data Scientist", "role_category": "ds_ml", "seniority": "junior",
        "seniority_source": "title", "stack_overlap": "strong", "employer_name": "Acme d.o.o.",
        "employer_type": "ru_with_serbian_entity", "serbian_entity": "yes", "serbian_entity_evidence": "d.o.o.",
        "employment": "official", "country": "Serbia", "city": "Belgrade", "work_format": "office",
        "ad_language": "ru", "required_languages": ["ru", "en"], "summary_ru": "SQL + Python, дашборды",
        "salary_text": "1500–2000 €", "confidence": "high"})
    verdict = ScoringEngine(config.rules).evaluate(facts, item.published_at, utcnow())
    vacancy_id = storage.vacancies.insert(run_id, NewVacancy(
        raw_id, "llm", "Junior Data Scientist", "Acme d.o.o.", "ds_ml", "junior", verdict.decision, None,
        item.published_at, facts, verdict))
    storage.vacancies.attach_source(run_id, vacancy_id, raw_id, "serbiawork", item.url)
    storage.llm_calls.record(LlmCallRecord(run_id, "scoring", "claude_sdk", "claude-haiku-5-5", 1, 10, 500, 9000,
                                           300, True, 3.0, auth_source="none"))
    storage.runs.commit(run_id, {"new_by_group": {"1": 1}, "new_total": 1, "errors": 0}, 30)
    app = create_app(config, storage, port=PORT)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as test_client:
        test_client.vacancy_id = vacancy_id
        yield test_client
    storage.db.close()


def test_pages_render(client):
    for path in ("/", "/processed", "/processed?filter=auto_skipped", "/health", "/tokens"):
        response = client.get(path)
        assert response.status_code == 200, path
    feed = client.get("/").text
    assert "Junior Data Scientist" in feed
    assert 'class="permit permit-yes">Yes<' in feed
    assert "Requires English" in feed, "non-Russian required languages are shown as a risk"
    assert "IT and data in Serbia" in feed
    assert "1500–2000 €" in feed
    assert "https://telegram.me/serbiawork/1" in feed
    assert "t.me/" not in feed.replace("telegram.me/", "")


def test_status_does_not_start_a_run(client):
    status = client.get("/api/status").json()
    assert status["running"] is False
    assert status["summary_line"].endswith("· new 1 (1 / 0 / 0 / 0 / 0)")
    assert client.get("/api/status").json()["running"] is False


def test_status_buttons_move_card_to_processed(client):
    vacancy_id = client.vacancy_id
    response = client.post(f"/api/vacancies/{vacancy_id}/status", json={"status": "not_now"}, headers=ORIGIN)
    assert response.status_code == 200
    assert response.json()["feed_count"] == 0
    assert "Junior Data Scientist" not in client.get("/").text
    assert "Junior Data Scientist" in client.get("/processed?filter=not_now").text
    client.post(f"/api/vacancies/{vacancy_id}/status", json={"status": "new"}, headers=ORIGIN)
    assert "Junior Data Scientist" in client.get("/").text


def test_copy_full_text_returns_original_text_and_link(client):
    text = client.get(f"/api/vacancies/{client.vacancy_id}/fulltext").json()["text"]
    assert "Полный текст вакансии" in text
    assert "https://telegram.me/serbiawork/1" in text


def test_post_without_local_origin_is_rejected(client):
    vacancy_id = client.vacancy_id
    assert client.post(f"/api/vacancies/{vacancy_id}/status", json={"status": "applied"}).status_code == 403
    evil = {"origin": "https://evil.example"}
    assert client.post("/api/run", headers=evil).status_code == 403
    assert client.post("/api/cancel", headers=evil).status_code == 403


def test_foreign_host_header_is_rejected(client):
    assert client.get("/", headers={"host": "evil.example"}).status_code == 403


def test_tokens_page_shows_stage_and_cost(client):
    page = client.get("/tokens").text
    assert "Evaluation" in page
    assert "Haiku 5.5" in page
    assert "Would cost via API" in page


def test_salary_without_numbers_is_not_shown():
    from vacancy_retriever.web.views import _salary

    assert _salary("По договорённости") == ""
    assert _salary("unknown") == ""
    assert _salary("1500–2000 €") == "1500–2000 €"


def test_model_labels_are_human_readable():
    from vacancy_retriever.web.views import model_label

    assert model_label("claude-haiku-5-5") == "Haiku 5.5"
    assert model_label("gpt-5.4-nano") == "GPT-5.4 nano"


def test_icon_fallback_for_telegram(client):
    response = client.get("/icons/serbiawork")
    assert response.status_code == 200
