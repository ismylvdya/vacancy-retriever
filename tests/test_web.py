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
        test_client.storage = storage
        yield test_client
    storage.db.close()


def test_pages_render(client):
    for path in ("/", "/processed", "/processed?filter=auto_skipped", "/statistics", "/statistics?view=runs",
                 "/health", "/tokens"):
        response = client.get(path)
        assert response.status_code == 200, path
    feed = client.get("/").text
    assert "Junior Data Scientist" in feed
    assert 'class="permit permit-yes">Yes<' in feed
    assert "<span>Junior</span>" in feed and "<span>Russian company with a Serbian d.o.o.</span>" in feed
    assert "Ad 🇷🇺" in feed and "Requires 🇬🇧 🇷🇺" in feed, "grade, employer type and language flags on the card"
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


def test_old_tabs_redirect_to_statistics(client):
    assert client.get("/health", follow_redirects=False).headers["location"] == "/statistics"
    assert client.get("/tokens", follow_redirects=False).headers["location"] == "/statistics?view=runs"


def test_statistics_runs_show_tokens_without_api_costs(client):
    page = client.get("/statistics?view=runs").text
    assert "Tokens (sdk-python)" in page
    assert "9,810" in page, "tokens of the run: 10 + 500 + 9000 + 300"
    assert "via API" not in page and "Evaluation" not in page


def test_statistics_sources_have_no_runs_table(client):
    page = client.get("/statistics").text
    assert "State after the latest run" in page
    assert "Tokens (sdk-python)" not in page


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


def test_statistics_runs_show_share_of_pro_limit(client):
    page = client.get("/statistics?view=runs").text
    assert "±% of 5h Pro Limit" in page
    assert "0.2%" in page, "9 810 / 4 500 000"


def test_processed_filters_and_run_groups(client):
    client.post(f"/api/vacancies/{client.vacancy_id}/status", json={"status": "skipped"}, headers=ORIGIN)
    page = client.get("/processed").text
    assert "Filters:" in page and 'name="country" value="Serbia"' in page
    assert "Run " in page and "Junior Data Scientist" in page
    page = client.get("/processed?country=Serbia&required_languages=en").text
    assert "Junior Data Scientist" in page and "1 matching" in page
    page = client.get("/processed?country=Serbia&country=Cyprus&ad_language=sr").text
    assert "Junior Data Scientist" not in page and "Nothing matches" in page
    page = client.get("/processed?filter=skipped&country=Serbia").text
    assert 'href="/processed?filter=applied&amp;country=Serbia"' in page, "sub-tabs keep the filters"


def test_processed_search_fields_and_filters(client):
    client.post(f"/api/vacancies/{client.vacancy_id}/status", json={"status": "skipped"}, headers=ORIGIN)
    assert "Junior Data Scientist" in client.get("/processed?q=data+SCIENTIST").text
    assert "Nothing matches" in client.get("/processed?q=nonexistent").text
    assert "Junior Data Scientist" in client.get("/processed?q=acme&in=employer").text
    assert "Nothing matches" in client.get("/processed?q=acme&in=title").text
    assert "Junior Data Scientist" in client.get("/processed?q=%D0%B4%D0%B0%D1%88%D0%B1%D0%BE%D1%80%D0%B4%D1%8B").text  # summary
    assert "Junior Data Scientist" in client.get("/processed?q=%D0%BF%D1%80%D0%BE+data+science&in=text").text
    assert "Nothing matches" in client.get("/processed?q=sql&in=text").text
    assert "Junior Data Scientist" in client.get("/processed?q=acme&country=Serbia").text
    assert "Nothing matches" in client.get("/processed?q=acme&country=Cyprus").text
    page = client.get("/processed?filter=skipped&q=acme&in=employer").text
    assert 'href="/processed?filter=applied&amp;q=acme&amp;in=employer"' in page, "sub-tabs keep the search"
    assert 'name="q" value="acme"' in page and '<option value="employer" selected>' in page


def test_source_options_are_alphabetical(config=None):
    from vacancy_retriever.web.views import apply_filters
    from vacancy_retriever.storage.vacancies import VacancySource
    import types
    names = {"b": "Beta", "a": "alpha", "c": "Charlie"}
    fake = types.SimpleNamespace(sources=types.SimpleNamespace(sources=[
        types.SimpleNamespace(id=i, name=n) for i, n in names.items()]))
    records = [types.SimpleNamespace(sources=[types.SimpleNamespace(source_id=i, sender=None)], skip_reason=None,
                                     group_no=None, stars=None, residence_permit=None, facts=None)
               for i in ["c", "c", "b", "a"]]
    _, filters = apply_filters(fake, records, {})
    source = next(f for f in filters if f.key == "source")
    assert [o.label for o in source.options] == ["alpha", "Beta", "Charlie"]


def test_debug_report_explains_the_verdict(client):
    info = client.get(f"/api/vacancies/{client.vacancy_id}/debug").json()
    assert info["id"] == client.vacancy_id
    assert any(line.startswith("Group 1: IT in Serbia") for line in info["explain"])
    assert f"Vacancy id {client.vacancy_id}" in info["report"] and '"country": "Serbia"' in info["report"]
    assert any("Serbia Work" in line or "serbiawork" in line for line in info["raw"])
    assert client.get("/api/vacancies/999999/debug").status_code == 404


def test_debug_controls_are_rendered_and_hidden_tab_works(client):
    feed = client.get("/").text
    assert 'id="debug-toggle"' in feed and 'data-action="copy-debug"' in feed
    run_id = client.storage.runs.start({})
    ad = RawItem("serbiawork", "2", "https://telegram.me/serbiawork/2", "Крипто-карта walllet.com", utcnow())
    raw_id = client.storage.items.insert(run_id, ad, "hidden", skip_reason="Advertisement")
    client.storage.runs.commit(run_id, {}, 1)
    page = client.get("/processed?filter=hidden").text
    assert f"raw {raw_id}" in page and "Advertisement" in page and "walllet.com" in page


def test_developer_notice_banner_and_dismiss(client, tmp_paths):
    from vacancy_retriever import notices

    notices.add_once(tmp_paths.notices, "first:x", "First letter: x", "Тема: «Новые вакансии»")
    page = client.get("/").text
    assert 'data-notice="first:x"' in page and "Copy text" in page and "Тема: «Новые вакансии»" in page
    assert client.post("/api/notices/first:x/dismiss", headers=ORIGIN).status_code == 200
    assert 'data-notice="first:x"' not in client.get("/").text


def test_email_vacancy_shows_sender_name_and_icon(client, tmp_paths):
    storage = client.storage
    run_id = storage.runs.start({})
    item = RawItem("gmail", "linkedin:42", "https://www.linkedin.com/jobs/view/42/", "Junior ML Engineer\nAcme · Belgrade",
                   utcnow() - timedelta(hours=2), title="Junior ML Engineer",
                   meta={"email": {"mailbox": "gmail", "uid": "5", "sender": "LinkedIn job alerts"}})
    raw_id = storage.items.insert(run_id, item, "scored")
    facts = VacancyFacts.model_validate({"title_original": "Junior ML Engineer", "role_category": "ds_ml",
                                         "country": "Serbia", "city": "Belgrade", "confidence": "high"})
    verdict = ScoringEngine(AppConfig(tmp_paths).rules).evaluate(facts, item.published_at, utcnow())
    vacancy_id = storage.vacancies.insert(run_id, NewVacancy(raw_id, "llm", "Junior ML Engineer", "Acme", "ds_ml",
                                                             "junior", verdict.decision, None, item.published_at,
                                                             facts, verdict))
    storage.vacancies.attach_source(run_id, vacancy_id, raw_id, "gmail", item.url)
    storage.runs.commit(run_id, {}, 1)
    feed = client.get("/").text
    assert 'class="src src-email src-from-gmail"' in feed and "LinkedIn job alerts" in feed
    assert 'src="/icons/mail-linkedin"' in feed
    assert client.get("/icons/mail-linkedin").status_code == 200
    assert client.get("/icons/mail-nothing").headers["content-type"].startswith("image/svg")


def test_feed_is_hidden_while_a_run_is_going(client):
    page = client.get("/").text
    assert 'class="empty-state run-placeholder"' in page and 'class="feed-content"' in page
    assert '<body data-tab="feed" class="">' in page


def test_run_panel_passes_period_and_source_groups(client, monkeypatch):
    from vacancy_retriever.pipeline.manager import RunManager

    started = []
    monkeypatch.setattr(RunManager, "start", lambda self, options: started.append(options))
    assert client.post("/api/run", headers=ORIGIN).status_code == 200
    assert started[-1].source_ids is None and started[-1].since is None
    response = client.post("/api/run", json={"days": 3, "groups": ["telegram", "email"]}, headers=ORIGIN)
    assert response.status_code == 200
    options = started[-1]
    assert options.since == timedelta(days=3)
    config = AppConfig()
    types = {s.type for s in config.sources.sources if s.id in options.source_ids}
    assert types == {"telegram", "email"}
    assert client.post("/api/run", json={"days": 99}, headers=ORIGIN).status_code == 400
    assert client.post("/api/run", json={"groups": []}, headers=ORIGIN).status_code == 400
    assert 'id="run-more"' in client.get("/").text


def test_salary_in_another_currency_shows_rub(client, tmp_paths):
    import json

    (tmp_paths.data / "rates").mkdir(exist_ok=True)
    (tmp_paths.data / "rates" / "2026-01-01.json").write_text(json.dumps({"RUB": 1, "EUR": 90.0}))
    feed = client.get("/").text
    assert "1500–2000 €" in feed and "≈ 135 000–180 000 ₽" in feed


def test_reports_and_a_reported_vacancy_leaves_the_feed_outside_debug(client):
    vacancy_id = client.vacancy_id
    page = client.get("/").text
    assert "skip-note" not in page and "why skip" not in page  # the optional Skip reason is gone (29.10.2026)
    assert client.post(f"/api/vacancies/{vacancy_id}/report", json={"text": "  "}, headers=ORIGIN).status_code == 400
    assert client.post("/api/vacancies/999999/report", json={"text": "x"}, headers=ORIGIN).status_code == 404
    response = client.post(f"/api/vacancies/{vacancy_id}/report",
                           json={"text": "Группа 1 неверна:\nэто удалёнка из РФ"}, headers=ORIGIN)
    assert response.json() == {"ok": True, "reported": 1}
    reported = client.get("/processed?filter=reported").text
    assert "Junior Data Scientist" in reported and "это удалёнка из РФ" in reported
    assert 'href="/processed?filter=reported"' in reported and "Reported" in reported
    assert "User report" in client.get(f"/api/vacancies/{vacancy_id}/debug").json()["report"]
    feed = client.get("/").text
    assert f'class="vac reported" id="vac-{vacancy_id}"' in feed  # hidden by CSS unless debug mode is on
    assert client.get("/api/status").json()["feed_count"] == 0


def test_runs_columns_by_coef_and_from_claude(client):
    run_id = client.storage.runs.history(1)[0]["id"]
    client.storage.db.execute("UPDATE runs SET real_limit_pct = 4 WHERE id = ?", (run_id,))  # typed before 31.10
    page = client.get("/statistics?view=runs").text
    assert "±% of 5h Pro Limit by COEF" in page and "% of 5h from CLAUDE" in page and ">4%</span>" in page
    assert "real % of 5h Pro Limit" not in page and 'class="real-limit"' not in page
    assert client.post(f"/api/runs/{run_id}/real-limit", json={"percent": 4}, headers=ORIGIN).status_code in (404, 405)


def test_missed_period_banner_include_and_dismiss(client, tmp_paths, monkeypatch):
    from vacancy_retriever.pipeline.manager import RunManager

    storage = Storage.open(tmp_paths.default_db, tmp_paths.token_log)
    assert 'id="missed"' not in client.get("/").text
    now = utcnow().replace(microsecond=0)  # stored to the second
    start, end = now - timedelta(days=10), now - timedelta(days=1)
    storage.runs.add_missed("serbiawork", start, end)
    page = client.get("/").text
    assert 'id="missed"' in page and "Not collected" in page and 'id="run-missed"' in page
    started = []
    monkeypatch.setattr(RunManager, "start", lambda self, options: started.append(options))
    client.post("/api/run", json={"days": 1, "include_missed": True}, headers=ORIGIN)
    assert started[-1].backfill == {"serbiawork": start}
    client.post("/api/run", json={"days": 1, "groups": ["sites"], "include_missed": True}, headers=ORIGIN)
    assert started[-1].backfill is None, "a Telegram hole is not collected by a sites-only run"
    assert client.post("/api/missed/dismiss", headers=ORIGIN).status_code == 200
    assert 'id="missed"' not in client.get("/").text


def test_drive_folder_picker_page(client):
    page = client.get("/drive/pick")
    assert page.status_code == 200 and "Choose a folder in Google Drive" in page.text
    assert client.post("/api/drive/picker", headers=ORIGIN).status_code == 409  # Drive / key not set up in tests
    assert client.post("/api/drive/parent", json={"id": "x"}, headers={"origin": "http://evil.example"}).status_code == 403


def test_feed_fit_threshold_defaults_to_four_stars(client):
    storage = client.storage
    stars = storage.vacancies.get(client.vacancy_id).stars
    assert stars >= 4  # the fixture vacancy is a good fit
    page = client.get("/").text
    assert "Junior Data Scientist" in page and 'name="stars" value="4" checked' in page and "Fit: <b>★4 and 5</b>" in page
    storage.db.execute("UPDATE vacancies SET stars = 3 WHERE id = ?", (client.vacancy_id,))
    page = client.get("/").text
    assert "Junior Data Scientist" not in page and "Nothing with these filters" in page
    assert "Junior Data Scientist" in client.get("/?stars=3").text and "Junior Data Scientist" in client.get("/?stars=0").text
    assert "Junior Data Scientist" not in client.get("/?stars=0&city=Novi Sad").text  # facets work with the threshold


def test_statistics_applies_tab(client):
    page = client.get("/statistics?view=applies").text
    assert "Applies" in page and "No applications yet" in page
    storage = client.storage
    app = storage.applications.create(client.vacancy_id, utcnow())
    storage.applications.update(app.id, state="done", stats_json='{"steps": {"note": 0.4, "materials": 41.2, "pdf": 3.1}}')
    storage.llm_calls.record(LlmCallRecord(None, "resume", "claude_sdk", "claude-sonnet-5-5", 1, 9000, 1200, 0, 0,
                                           True, 41.0, application_id=app.id))
    page = client.get("/statistics?view=applies").text
    assert "Junior Data Scientist" in page and "CV + letter (Claude) 41s" in page and "10,200" in page


def test_linkedin_description_share_in_statistics(client):
    storage = client.storage
    run_id = storage.runs.start({})
    for n, detail in ((1, "Data Analyst\nDescription source: Google for Jobs (via LinkedIn)\n\ntext"), (2, None)):
        raw = storage.items.insert(run_id, RawItem("gmail", f"linkedin:{n}", f"https://www.linkedin.com/jobs/view/{n}/",
                                                   "letter", utcnow()), "scored")
        if detail:
            storage.items.set_detail(raw, detail)
    from vacancy_retriever.storage.runs import SourceRunRecord
    storage.runs.record_source(run_id, SourceRunRecord("gmail", "ok", collected=2))
    storage.runs.commit(run_id, {"new_total": 0}, 30)
    page = client.get("/statistics").text
    assert "LinkedIn descriptions: 1 of 2 found via the job API (50%)" in page and "All time: 1 of 2" in page


def test_claude_limit_bars_in_the_footer(client, tmp_paths):
    import json
    import time

    assert "never — press ↻" in client.get("/").text
    later = time.time() + 2 * 3600 + 600
    (tmp_paths.data / "claude_limits.json").write_text(json.dumps({"observed_at": time.time(), "windows": {
        "five_hour": {"utilization": 0.58, "resetsAt": later}, "seven_day": {"utilization": 0.37, "resetsAt": later}}}))
    page = client.get("/").text
    assert "5-hour limit" in page and "58%" in page and "Resets in 2 h" in page and "Weekly · all models" in page
    assert client.get("/api/status").json()["limits"]["bars"][1]["percent"] == 37
