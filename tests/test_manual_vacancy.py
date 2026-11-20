"""A vacancy the user found himself (20.11.2026, apply/manual.py): «+ Add vacancy» → the same Apply pipeline.
Title, link and description are typed by the user; no page is opened and no LLM reads them."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vacancy_retriever.apply.manual import ManualError, ManualIntake
from vacancy_retriever.apply.models import ApplicationState
from vacancy_retriever.apply.service import ApplicationWorker
from vacancy_retriever.web.app import create_app

from .test_apply import ORIGIN, PORT, env  # noqa: F401 - the fixture

URL = "https://careers.acme.example/jobs/42"
TEXT = "Python, SQL, CatBoost. Офис в Белграде, команда русскоязычная."


def add_and_run(e, cv: bool = True, letter: bool = True, company: str = "Acme d.o.o."):
    added = ManualIntake(e.storage).create("Junior Data Scientist", URL, TEXT, company)
    assert e.service.generate(added.application.id, cv, letter)
    worker = ApplicationWorker(e.storage.applications, e.pipeline)
    while worker.run_once():
        pass
    return added, e.storage.applications.get(added.application.id)


def test_typed_vacancy_gets_the_note_folder_cv_and_letter(env):  # noqa: F811
    added, app = add_and_run(env)
    assert app.state == ApplicationState.DONE, app.error
    vacancy = env.storage.vacancies.get(added.vacancy_id)
    assert vacancy.user_status == "applied" and vacancy.employer == "Acme d.o.o."
    assert added.vacancy_id not in {v.id for v in env.storage.vacancies.feed()}, "never in the feed"
    note = Path(app.note_path)
    assert note.name == "Acme d.o.o. • Junior Data Scientist 🦆.md"
    text = note.read_text(encoding="utf-8")
    assert f"]({URL})" in text and "CatBoost" in text and "[папка на Google Drive]" in text
    assert app.folder_url and app.pdf_path and "ВСТАВИТЬ" not in app.letter
    assert [c["stage"] for c in env.storage.llm_calls.all()] == ["resume"], "only the usual CV + letter call"


def test_without_company_and_note_only(env):  # noqa: F811
    _, app = add_and_run(env, cv=False, letter=True, company="")
    assert app.state == ApplicationState.DONE and not app.pdf_path and not app.folder_id and not app.letter
    assert Path(app.note_path).name == "Junior Data Scientist 🦆.md"


@pytest.mark.parametrize(("title", "url", "text", "message"), [
    ("", URL, TEXT, "title"), ("Data Analyst", "not a link", TEXT, "link"), ("Data Analyst", URL, "  ", "description")])
def test_all_three_fields_are_required(env, title, url, text, message):  # noqa: F811
    with pytest.raises(ManualError, match=message):
        ManualIntake(env.storage).create(title, url, text)


def test_api_adds_and_queues(env):  # noqa: F811
    client = TestClient(create_app(env.config, env.storage, PORT, applications=env.service),
                        base_url=f"http://127.0.0.1:{PORT}")
    response = client.post("/api/applications/manual", json={
        "title": "Junior Data Scientist", "url": URL, "description": TEXT, "cv": True, "letter": False}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    app = env.storage.applications.get(response.json()["application_id"])
    assert app.state == ApplicationState.QUEUED and app.with_cv and not app.with_letter
    bad = client.post("/api/applications/manual", json={"title": "x", "url": "nope", "description": "y"},
                      headers=ORIGIN)
    assert bad.status_code == 400 and "link" in bad.json()["detail"]
    page = client.get("/").text
    assert 'id="add-vacancy"' in page and 'id="manual-title"' in page and 'id="manual-text"' in page
