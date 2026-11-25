"""A vacancy the user found himself (20.11.2026, apply/manual.py): «+ Add vacancy» → the same Apply pipeline.
Title, link and description are typed by the user; no page is opened and no LLM reads them."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vacancy_retriever.apply.drive import file_url
from vacancy_retriever.apply.manual import ManualError, ManualIntake
from vacancy_retriever.apply.models import ApplicationState
from vacancy_retriever.apply.service import ApplicationWorker
from vacancy_retriever.web.app import create_app

from .test_apply import ORIGIN, PORT, env, run_all  # noqa: F401 - the fixture

URL = "https://careers.acme.example/jobs/42"
TEXT = "Python, SQL, CatBoost. Офис в Белграде, команда русскоязычная."


def add_and_run(e, cv: bool = True, letter: bool = True, company: str = "Acme d.o.o."):
    added = ManualIntake(e.storage).create("Junior Data Scientist", URL, TEXT, company, cv=cv, letter=letter)
    assert added.application.state == ApplicationState.QUEUED
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
    _, app = add_and_run(env, cv=False, letter=False, company="")
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

@pytest.mark.parametrize("generate_cv, generate_letter, ready_cv, ready_letter", [
    (False, False, False, False),
    (False, False, True, True),
    (False, False, True, False),
    (False, False, False, True),
    (True, False, False, True),
    (False, True, True, False),
    (False, True, False, False),
])
def test_manual_vacancy_with_existing_materials(env, tmp_path, monkeypatch,
                                               generate_cv, generate_letter, ready_cv, ready_letter):
    pdf = tmp_path / "My résumé.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr("vacancy_retriever.apply.manual.choose_pdf", lambda: pdf)
    revealed = []
    monkeypatch.setattr("vacancy_retriever.web.app.reveal_in_finder", lambda path: revealed.append(path) or True)
    app = create_app(env.config, env.storage, port=PORT, applications=env.service)
    supplied = "Письмо для «Acme».\n\nВторой абзац."
    expected_letter = 'Письмо для "Acme".\n\nВторой абзац.'
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        token = None
        if ready_cv:
            reply = client.post("/api/applications/manual/cv", headers=ORIGIN)
            assert reply.json()["file"]["name"] == pdf.name and str(pdf) not in reply.text
            token = reply.json()["file"]["token"]
            assert client.post(f"/api/applications/manual/cv/{token}/reveal", headers=ORIGIN).status_code == 200
            assert revealed == [pdf]
        response = client.post("/api/applications/manual", headers=ORIGIN, json={
            "title": "Junior Data Scientist", "company": "Acme", "url": "https://example.test/vacancy",
            "description": "Data science and SQL", "cv": generate_cv, "letter": generate_letter,
            "pdf_token": token, "cover_letter": supplied if ready_letter else None,
        })
        assert response.status_code == 200, response.text
        env.vacancy_id = response.json()["vacancy_id"]
        if token:
            path = f"/api/applications/manual/cv/{token}"
            assert client.delete(path, headers=ORIGIN).status_code == 200
            assert client.post(f"{path}/reveal", headers=ORIGIN).status_code == 404
        done = run_all(env)
        assert done.state == ApplicationState.DONE and done.warning is None
        assert env.provider.calls == int(generate_cv or generate_letter)
        assert env.skill.builds == int(generate_cv)
        assert env.drive.folders == int(generate_cv or ready_cv)
        assert env.drive.uploads == int(generate_cv or ready_cv)
        assert done.note_path
        text = Path(done.note_path).read_text()
        assert ("### моё" in text) == any((generate_cv, generate_letter, ready_cv, ready_letter))
        if ready_cv:
            assert done.pdf_path == str(pdf) and done.pdf_url == file_url("P1")
            assert f"[папка на Google Drive]({done.folder_url})" in text
            assert "[резюме PDF]" not in text
            assert (done.pdf_url in text) == generate_letter
        if ready_letter:
            assert done.letter == expected_letter and expected_letter in text
        if generate_letter:
            assert done.letter and "ВСТАВИТЬ" not in done.letter
            assert (file_url("P1") in done.letter) == ready_cv
