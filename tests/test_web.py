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
    assert "Ad Russian" in feed and "Requires English, Russian" in feed, "grade, employer type and languages on the card"
    assert 'flag flag-ru">Russian-speaking<' in feed and "🇬🇧" not in feed
    assert '<details class="parsed-description" open>' in feed and "The post / card text" in feed
    assert "IT and data in Serbia" in feed
    assert "1500–2000 €" in feed
    assert "https://telegram.me/serbiawork/1" in feed
    assert "t.me/" not in feed.replace("telegram.me/", "")


def test_status_does_not_start_a_run(client):
    status = client.get("/api/status").json()
    assert status["running"] is False
    assert status["summary_line"].endswith("· new 1 (0 / 0 / 1 / 0 / 0 / 0 / 0 / 0)"), "groups in the feed's order: 7, 6, 1…"
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
    assert 'class="filters-label">Filters<' in page and 'name="country" value="Serbia"' in page
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
        types.SimpleNamespace(id=i, name=n) for i, n in names.items()]),
        mail=types.SimpleNamespace(senders=[types.SimpleNamespace(name="hh.ru", icon_id="mail-hh")]))
    records = [types.SimpleNamespace(sources=[types.SimpleNamespace(source_id=i, sender=None)], skip_reason=None,
                                     group_no=None, stars=None, residence_permit=None, facts=None)
               for i in ["c", "c", "b", "a"]]
    _, filters = apply_filters(fake, records, {})
    source = next(f for f in filters if f.key == "source")
    assert [o.label for o in source.options] == ["alpha", "Beta", "Charlie"]
    assert [o.icon for o in source.options] == ["a", "b", "c"]  # the source's own icon in the dropdown
    letters = [types.SimpleNamespace(sources=[types.SimpleNamespace(source_id="gmail", sender="hh.ru")], skip_reason=None,
                                     group_no=None, stars=None, residence_permit=None, facts=None)]
    _, filters = apply_filters(fake, letters, {})
    assert next(f for f in filters if f.key == "source").options[0].icon == "mail-hh"  # the sender's icon


def test_debug_report_explains_the_verdict(client):
    info = client.get(f"/api/vacancies/{client.vacancy_id}/debug").json()
    assert info["id"] == client.vacancy_id
    assert any(line.startswith("Group 1: IT in Serbia") for line in info["explain"])
    assert '"country": "Serbia"' in info["facts"] and "report" not in info
    assert any("Serbia Work" in line or "serbiawork" in line for line in info["raw"])
    assert client.get("/api/vacancies/999999/debug").status_code == 404


def test_debug_controls_are_rendered_and_hidden_tab_works(client):
    feed = client.get("/").text
    assert 'id="debug-toggle"' in feed and "copy-debug" not in feed and "Copy text" not in feed and "Copy id" not in feed
    assert f'data-action="copy-id" title="Copy the id">#{client.vacancy_id}</button>' in feed
    assert "Run " in feed  # the debug line with the run's date
    assert feed.index('data-action="report"') < feed.index('class="debug-panel"')
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
    response = client.post("/api/run", json={"vacancy_groups": [6, 3]}, headers=ORIGIN)
    assert response.status_code == 200 and started[-1].vacancy_groups == [3, 6], "feed groups of this run"
    assert client.post("/api/run", json={"vacancy_groups": []}, headers=ORIGIN).status_code == 400
    assert client.post("/api/run", json={"vacancy_groups": [9]}, headers=ORIGIN).status_code == 400
    page = client.get("/").text
    assert 'id="run-more"' in page and 'class="run-vgroups"' in page and "Remote in Russia</label>" in page


def test_salary_in_another_currency_shows_rub(client, tmp_paths):
    import json

    (tmp_paths.data / "rates").mkdir(exist_ok=True)
    (tmp_paths.data / "rates" / "2026-01-01.json").write_text(json.dumps({"RUB": 1, "EUR": 90.0}))
    feed = client.get("/").text
    assert "1500–2000 €" in feed and "≈ 135 000–180 000 ₽" in feed


def test_reported_vacancy_stays_in_feed_until_status_changes(client):
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
    assert 'data-report="Группа 1 неверна:' in reported, "Report opens what was written"
    assert client.post(f"/api/vacancies/{vacancy_id}/report", json={"text": "исправлено"}, headers=ORIGIN).json()["reported"] == 1
    assert len(client.storage.vacancies.reports()[vacancy_id]) == 1, "editing does not add a second report"
    edited = client.get("/processed?filter=reported").text
    assert "исправлено" in edited and "это удалёнка из РФ" not in edited
    feed = client.get("/").text
    assert f'class="vac" id="vac-{vacancy_id}"' in feed
    assert 'class="debug-only flag reported"' in feed
    assert client.get("/api/status").json()["feed_count"] == 1
    skipped = client.post(f"/api/vacancies/{vacancy_id}/status", json={"status": "skipped"}, headers=ORIGIN)
    assert skipped.json()["feed_count"] == 0
    assert f'id="vac-{vacancy_id}"' not in client.get("/").text
    assert "исправлено" in client.get("/processed?filter=reported").text
    restored = client.post(f"/api/vacancies/{vacancy_id}/status", json={"status": "new"}, headers=ORIGIN)
    assert restored.json()["feed_count"] == 1
    assert f'id="vac-{vacancy_id}"' in client.get("/").text


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
    for n, detail, status in ((1, "Data Analyst\nDescription source: Google for Jobs (via LinkedIn)\n\ntext", "scored"),
                              (2, "Data Analyst\nDescription source: LinkedIn public page\n\ntext", "scored"),
                              (3, None, "scored"), (4, None, "waiting_page")):
        raw = storage.items.insert(run_id, RawItem("gmail", f"linkedin:{n}", f"https://www.linkedin.com/jobs/view/{n}/",
                                                   "letter", utcnow()), status)
        if detail:
            storage.items.set_detail(raw, detail)
    from vacancy_retriever.storage.runs import SourceRunRecord
    storage.runs.record_source(run_id, SourceRunRecord("gmail", "ok", collected=2))
    storage.runs.commit(run_id, {"new_total": 0}, 30)
    page = client.get("/statistics").text
    from vacancy_retriever.web.views import DescriptionSplit, linkedin_descriptions

    assert linkedin_descriptions(storage)[(run_id, "gmail")] == DescriptionSplit(google=1, page=1, none=1, waiting=1)
    assert "LinkedIn descriptions, this run" in page and "All time" in page
    for label in ("Google for Jobs", "LinkedIn page", "Not found", "Waiting for the page"):
        assert f"<span>{label}</span><b>25%</b>" in page




def test_unknown_or_skipped_filter_in_the_address_is_ignored(client):
    for query in ("?fit=5", "?reason=x", "?nonsense=1&stars=0"):
        assert client.get(f"/{query}").status_code == 200, query
    assert client.get("/processed?nonsense=1").status_code == 200


def set_status_at(client, vacancy_id, status, when):
    client.storage.db.execute("UPDATE vacancies SET user_status = ?, status_changed_at = ? WHERE id = ?",
                              (status, when, vacancy_id))


def test_all_manually_is_only_what_the_user_did(client):
    vacancy_id = client.vacancy_id
    set_status_at(client, vacancy_id, "auto_skipped", "2026-10-20T10:00:00+00:00")
    page = client.get("/processed").text
    assert ">All manually<" in page and "Junior Data Scientist" not in page
    assert 'class="seg debug-only seg-pale' in page and "Auto-skipped" in page
    assert "Junior Data Scientist" in client.get("/processed?filter=auto_skipped").text
    set_status_at(client, vacancy_id, "skipped", "2026-10-20T10:00:00+00:00")
    assert "Junior Data Scientist" in client.get("/processed").text
    set_status_at(client, vacancy_id, "new", None)
    assert "Junior Data Scientist" not in client.get("/processed").text
    client.storage.vacancies.save_report(vacancy_id, "неверно")
    assert "Junior Data Scientist" in client.get("/processed").text, "a report is a manual action too"


def test_manual_tabs_group_by_the_day_of_the_action_auto_skipped_by_run(client):
    set_status_at(client, client.vacancy_id, "applied", "2026-10-20T10:00:00+00:00")
    for path in ("/processed", "/processed?filter=applied"):
        page = client.get(path).text
        assert "<h2>20 Oct 2026</h2>" in page and "<h2>Run " not in page
    set_status_at(client, client.vacancy_id, "auto_skipped", "2026-10-20T10:00:00+00:00")
    page = client.get("/processed?filter=auto_skipped").text
    assert "<h2>Run " in page and "<h2>20 Oct 2026</h2>" not in page
    set_status_at(client, client.vacancy_id, "new", None)
    client.storage.db.execute("INSERT INTO reports(vacancy_id, text, created_at) VALUES (?, 'x', '2026-10-21T09:00:00+00:00')",
                              (client.vacancy_id,))
    assert "<h2>21 Oct 2026</h2>" in client.get("/processed?filter=reported").text


def test_sidebar_title_opens_the_card_on_its_tab(client):
    from vacancy_retriever.web.views import _card_url

    set_status_at(client, client.vacancy_id, "applied", "2026-10-20T10:00:00+00:00")
    record = client.storage.vacancies.get(client.vacancy_id)
    assert _card_url(record) == f"/processed?filter=applied#vac-{client.vacancy_id}"
    set_status_at(client, client.vacancy_id, "not_now", "2026-10-20T10:00:00+00:00")
    assert _card_url(client.storage.vacancies.get(client.vacancy_id)) == f"/processed?filter=not_now#vac-{client.vacancy_id}"
    assert _card_url(None) is None


def test_linkedin_switch_endpoints(client):
    settings = client.get("/api/settings").json()
    assert settings["linkedin_pages"]["enabled"] is False and "mail:linkedin_public_pages.enabled" not in settings["values"]
    refused = client.post("/api/settings/linkedin-pages/on", json={"confirm": "ENABLE LINKEDIN"}, headers=ORIGIN)
    assert refused.status_code == 422 and "No proxy" in refused.json()["detail"]
    assert client.post("/api/settings/linkedin-pages/on", json={}, headers=ORIGIN).status_code == 422
    assert client.post("/api/settings/linkedin-pages/on", json={"confirm": "ENABLE LINKEDIN"}).status_code == 403, "same origin"
    assert client.post("/api/settings/linkedin-pages/off", headers=ORIGIN).json()["enabled"] is False
    assert client.get("/api/settings").json()["linkedin_pages"]["enabled"] is False

