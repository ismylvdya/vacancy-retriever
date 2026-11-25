from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from vacancy_retriever.apply.drive import DriveItem, NoDrive, StubDrive, file_url, folder_url
from vacancy_retriever.apply.models import ApplicationState, ApplyError, Materials
from vacancy_retriever.apply.obsidian import ObsidianVault, frontmatter, normalize_company, note_file_name
from vacancy_retriever.apply.pipeline import ApplicationPipeline, ApplyServices, DossierBuilder, english_title
from vacancy_retriever.apply.resume import MaterialsWriter, ResumeSkill, insert_cv_link
from vacancy_retriever.apply.service import ApplicationService, ApplicationWorker
from vacancy_retriever.collectors.base import RawItem
from vacancy_retriever.collectors.job_search import employer_links
from vacancy_retriever.config import AppConfig, ObsidianSettings
from vacancy_retriever.llm.base import LlmError, LlmProvider, LlmResponse, LlmUsage
from vacancy_retriever.scoring.engine import ScoringEngine
from vacancy_retriever.scoring.facts import VacancyFacts
from vacancy_retriever.storage import Storage
from vacancy_retriever.storage.vacancies import NewVacancy
from vacancy_retriever.timeutil import utcnow
from vacancy_retriever.web.app import create_app
from vacancy_retriever.web.views import application_item

PORT = 18765
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}
LETTER = ("Представитель компании Acme, добрый день!\n\nТекст.\n\n"
          "Резюме: ❗️❗️❗️❗️❗️❗️❗️ВСТАВИТЬ❗️❗️❗️❗️❗️❗️❗️\nGitHub: https://github.com/x")
REPLY = {"selection": {"lang": "ru", "company": "Acme", "role_abbr": "DS", "title": "Junior Data Scientist"},
         "letter": LETTER, "skills": ["Python", "SQL", "A/B testing"]}


def seed_vacancy(config: AppConfig, storage: Storage, text: str = "Полный текст вакансии про Data Science") -> int:
    run_id = storage.runs.start({})
    item = RawItem("serbiawork", "1", "https://telegram.me/serbiawork/1", text, utcnow() - timedelta(hours=3))
    raw_id = storage.items.insert(run_id, item, "scored")
    facts = VacancyFacts.model_validate({
        "title_original": "Junior Data Scientist", "role_category": "ds_ml", "seniority": "junior",
        "employer_name": "Acme d.o.o.", "employer_type": "ru_with_serbian_entity", "country": "Serbia",
        "city": "Belgrade", "work_format": "office", "ad_language": "ru", "confidence": "high"})
    verdict = ScoringEngine(config.rules).evaluate(facts, item.published_at, utcnow())
    vacancy_id = storage.vacancies.insert(run_id, NewVacancy(
        raw_id, "llm", "Junior Data Scientist", "Acme d.o.o.", "ds_ml", "junior", verdict.decision, None,
        item.published_at, facts, verdict))
    storage.vacancies.attach_source(run_id, vacancy_id, raw_id, "serbiawork", item.url)
    storage.runs.commit(run_id, {"new_by_group": {}, "new_total": 1, "errors": 0}, 30)
    return vacancy_id


class FakeProvider(LlmProvider):
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.fail: LlmError | None = None

    async def complete_json(self, system: str, user: str, model: str, schema: dict[str, Any] | None = None) -> LlmResponse:
        self.calls += 1
        # the CV + letter call carries the skill; the Not now call only asks for the vacancy's skills
        assert ("SKILL" in system and "CATALOG" in system or "key hard skills" in system) and "Junior Data Scientist" in user
        if self.fail:
            raise self.fail
        return LlmResponse(REPLY, LlmUsage(1000, 200, model="claude-sonnet-5-5"), "")


class FakeSkill:
    def __init__(self) -> None:
        self.builds = 0

    def instructions(self) -> str:
        return "SKILL"

    def catalog(self) -> str:
        return "CATALOG"

    def build(self, selection: dict[str, Any], out_dir: Path) -> tuple[Path, str | None]:
        self.builds += 1
        out_dir.mkdir(parents=True, exist_ok=True)
        pdf = out_dir / f"CV_{selection['company']}_{selection['role_abbr']}.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        return pdf, None


class FakeDrive(NoDrive):
    def __init__(self) -> None:
        super().__init__("")
        self.folders = 0
        self.uploads = 0
        self.folder_ids: dict[int | str, str] = {}

    @property
    def available(self) -> bool:
        return True

    def ensure_folder(self, key: int | str, name: str) -> DriveItem:
        self.folders += 1
        self.names = [*getattr(self, "names", []), name]
        folder_id = self.folder_ids.setdefault(key, f"F{len(self.folder_ids) + 1}")
        return DriveItem(folder_id, folder_url(folder_id))

    def upload_pdf(self, key: int | str, folder_id: str, path: Path) -> DriveItem:
        self.uploads += 1
        file_id = f"P{folder_id[1:]}"
        return DriveItem(file_id, file_url(file_id))


@pytest.fixture
def env(tmp_paths, tmp_path):
    config = AppConfig(tmp_paths)
    storage = Storage.open(tmp_paths.default_db, tmp_paths.token_log)
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "Acme (IT).md").write_text(
        "---\ntags:\n  - 👾травля\n👾что_это:\n  - 🧔🏻‍♂️ работодатель\naliases:\n  - Acme Group\n---\n", encoding="utf-8")
    provider, skill, drive = FakeProvider(), FakeSkill(), FakeDrive()
    vault = ObsidianVault(vault_dir, config.apply.obsidian)
    writer = MaterialsWriter(provider, "sonnet", skill, storage.llm_calls)  # type: ignore[arg-type]
    services = ApplyServices(DossierBuilder(config, storage), vault, drive, writer, skill,  # type: ignore[arg-type]
                             tmp_paths.data / "applications")
    pipeline = ApplicationPipeline(storage, services, retry_minutes=30)
    service = ApplicationService(storage.applications, True, 0, ApplicationWorker(storage.applications, pipeline))

    class Env:
        pass

    e = Env()
    e.__dict__.update(config=config, storage=storage, provider=provider, skill=skill, drive=drive, vault=vault,
                      pipeline=pipeline, service=service, services=services, vacancy_id=seed_vacancy(config, storage))
    yield e
    storage.db.close()


def run_all(e) -> Any:
    worker = ApplicationWorker(e.storage.applications, e.pipeline)
    while worker.run_once():
        pass
    return e.storage.applications.for_vacancy(e.vacancy_id)


def apply_now(e, vacancy_id: int | None = None, cv: bool = True, letter: bool = True) -> Any:
    """▶ (the vacancy waits in the sidebar's purgatory), then «Generate Obsidian note» with these boxes checked."""
    app = e.service.enqueue(vacancy_id or e.vacancy_id)
    assert app.state == ApplicationState.DRAFT, "▶ alone makes nothing (18.11.2026)"
    assert e.service.generate(app.id, cv, letter)
    return e.storage.applications.get(app.id)


def test_full_pipeline_makes_note_cv_letter_and_folder(env):
    apply_now(env)
    app = run_all(env)
    assert app.state == ApplicationState.DONE and app.error is None and app.warning is None
    assert app.letter.endswith(f"Резюме: {file_url('P1')}\nGitHub: https://github.com/x")
    assert Path(app.pdf_path).exists() and app.folder_url == folder_url("F1")
    note = Path(app.note_path)
    assert note.name == "Acme d.o.o. • Junior Data Scientist 🦆.md"
    text = note.read_text(encoding="utf-8")
    meta = frontmatter(text)
    assert meta["tags"] == ["👾травля"] and meta["👾что_это"] == ["📋 вакансия"]
    assert meta["👾лэйблы"] is None, "made by the app: the 🦆 in the name says it, no label (29.10.2026)"
    assert meta["👾где"] == ["🇷🇺яз: в Сербии очно"] and meta["retriever_id"] == env.vacancy_id
    assert meta["👾FK"] == "[[Acme (IT)]]", "employer note found by its name without the legal suffix"
    assert meta["👾необходимые скилы"] == "Python, SQL, A/B testing"
    assert meta["👾статус"] == ["откликнулся"] and str(meta["начало"]) == date.today().isoformat()
    assert "%%" not in text, "no service markers in the note"
    assert text.index("### описание вакансии") < text.index("https://telegram.me/serbiawork/1") < text.index("### моё")
    assert "https://telegram.me/serbiawork/1" in text and "Полный текст вакансии про Data Science" in text
    assert f"### моё\n[папка на Google Drive]({folder_url('F1')})\n\n**cover letter:**\n```" in text
    assert file_url("P1") in text
    assert "### описание вакансии\n[эта вакансия в Telegram-канале " in text
    assert "](https://telegram.me/serbiawork/1)" in text and "\n\n**описание вакансии:**\n```" in text
    assert "ВСТАВИТЬ" not in text
    assert env.drive.names == [f"Acme d.o.o. • Junior Data Scientist • {date.today().isoformat()} 🦆"]
    calls = env.storage.llm_calls.all()
    assert [c["stage"] for c in calls] == ["resume"] and calls[0]["input_tokens"] == 1000


def test_claude_limit_waits_then_resumes_without_redoing_steps(env):
    env.provider.fail = LlmError("usage limit reached", kind="limit", retryable=False)
    apply_now(env)
    app = run_all(env)
    assert app.state == ApplicationState.WAITING and app.step == "materials"
    assert app.due_at > utcnow() + timedelta(minutes=29) and app.note_path and app.folder_id
    env.provider.fail = None
    assert env.service.retry(app.id)
    app = run_all(env)
    assert app.state == ApplicationState.DONE
    assert env.drive.folders == 1 and env.provider.calls == 2 and env.skill.builds == 1
    assert len(list(env.vault.root.glob("*Junior*.md"))) == 1, "the note is not created twice"


def test_restart_continues_interrupted_work(env):
    app = apply_now(env)
    env.storage.applications.update(app.id, state=ApplicationState.WORKING, step="pdf",
                                    materials_json=Materials(REPLY["selection"], LETTER, ["Python"]).to_json())
    assert env.storage.applications.recover() == 1
    app = run_all(env)
    assert app.state == ApplicationState.DONE and env.provider.calls == 0, "the LLM result was already stored"


def test_a_taken_note_name_gets_a_number(env):
    taken = env.vault.root / "Acme d.o.o. • Junior Data Scientist 🦆.md"
    own_note = f"---\nretriever_id: 0\n---\nmy own note\nretriever_id: {env.vacancy_id}\n"
    taken.write_text(own_note, encoding="utf-8")
    apply_now(env)
    app = run_all(env)
    assert Path(app.note_path).name == "Acme d.o.o. • Junior Data Scientist 2 🦆.md"
    assert taken.read_text(encoding="utf-8") == own_note


def test_user_edits_in_the_note_are_kept(env):
    apply_now(env)
    app = run_all(env)
    note = Path(app.note_path)
    renamed = note.with_name("Моё имя.md")
    note.rename(renamed)
    text = renamed.read_text(encoding="utf-8").replace("👾необходимые скилы: \"Python, SQL, A/B testing\"",
                                                       "👾необходимые скилы: мои")
    text = text.replace("### моё\n", "### моё\nМои мысли\n")
    renamed.write_text(text, encoding="utf-8")
    env.storage.applications.update(app.id, skills="Go", pdf_url="https://drive/new")
    env.service.retry(app.id)
    app = run_all(env)
    text = renamed.read_text(encoding="utf-8")
    assert app.note_path == str(renamed), "found again by retriever_id"
    assert "👾необходимые скилы: мои" in text and "Мои мысли" in text and "https://drive/new" not in text
    assert "edited by hand" in app.warning


def test_cv_pdf_goes_to_downloads_and_is_rebuilt_without_llm(env):
    apply_now(env)
    app = run_all(env)
    pdf = Path(app.pdf_path)
    assert pdf.parent == env.services.out_dir and pdf.exists()  # ~/Downloads in the app, a temp folder here
    assert app.note_mine and "### моё" not in app.note_mine  # what went into «### моё», kept in the database
    pdf.unlink()  # the user deleted it from Downloads
    env.service.retry(app.id)
    app = run_all(env)
    assert Path(app.pdf_path).exists() and env.provider.calls == 1 and env.skill.builds == 2


def test_a_taken_downloads_name_gets_a_number(tmp_path):
    from vacancy_retriever.apply.pipeline import downloads_path

    (tmp_path / "Ismailov_CV_X_DA.pdf").write_bytes(b"someone else's")
    assert downloads_path(tmp_path, "Ismailov_CV_X_DA.pdf", None).name == "Ismailov_CV_X_DA 2.pdf"
    own = str(tmp_path / "Ismailov_CV_X_DA.pdf")
    assert downloads_path(tmp_path, "Ismailov_CV_X_DA.pdf", own).name == "Ismailov_CV_X_DA.pdf"  # its own: replaced


def test_mine_section_follows_the_app_until_the_user_edits_it(env):
    env.services.drive = NoDrive("Google Drive not connected")
    apply_now(env)
    app = run_all(env)
    note = Path(app.note_path)
    assert "Google Drive не подключён" in note.read_text(encoding="utf-8")
    env.services.drive = env.drive  # Drive connected later: Retry puts the real links in
    env.service.retry(app.id)
    app = run_all(env)
    text = note.read_text(encoding="utf-8")
    assert f"[папка на Google Drive]({folder_url('F1')})" in text and file_url("P1") in text
    assert "Google Drive не подключён" not in text and text.count("### моё") == 1


def test_without_drive_the_rest_is_done_and_letter_keeps_the_marker(env):
    env.services.drive = NoDrive("Google Drive not connected")
    apply_now(env)
    app = run_all(env)
    assert app.state == ApplicationState.DONE and app.warning == "Google Drive not connected"
    assert "ВСТАВИТЬ" in app.letter and app.folder_url is None
    assert "Google Drive не подключён" in Path(app.note_path).read_text(encoding="utf-8")


def test_drive_stub_gives_obviously_fake_links_and_a_warning(env):
    env.services.drive = StubDrive()
    apply_now(env)
    app = run_all(env)
    assert app.state == ApplicationState.DONE and "stub" in app.warning
    assert app.folder_url.startswith("https://drive-stub.invalid/") and "ВСТАВИТЬ" not in app.letter
    assert "https://drive-stub.invalid/" in app.letter and "drive.google.com" not in app.letter


def test_setup_error_fails_with_retry(env):
    def broken(selection, out_dir):
        raise ApplyError("xelatex not found", kind="setup")

    env.skill.build = broken
    apply_now(env)
    app = run_all(env)
    assert app.state == ApplicationState.FAILED and app.step == "pdf" and "xelatex" in app.error


def test_undo_and_stop(env):
    first = env.service.enqueue(env.vacancy_id)
    assert first.state == ApplicationState.DRAFT and env.storage.applications.next_due(utcnow()) is None
    env.service.withdraw(env.vacancy_id)  # Undo while in the purgatory: nothing was made, the row goes
    assert env.storage.applications.for_vacancy(env.vacancy_id) is None
    app = apply_now(env)
    env.storage.applications.update(app.id, state=ApplicationState.WORKING)
    assert env.service.stop(app.id) and env.storage.applications.get(app.id).cancel_requested  # after this step
    env.storage.applications.update(app.id, state=ApplicationState.FAILED, note_path="/x.md", cancel_requested=0)
    env.service.withdraw(env.vacancy_id)
    assert env.storage.applications.get(app.id).state == ApplicationState.CANCELLED, "started: stays as Stopped"
    env.service.enqueue(env.vacancy_id)
    assert env.storage.applications.get(app.id).state == ApplicationState.DRAFT, "▶ again: back to the purgatory"


def test_not_now_starts_after_the_undo_window(env):
    service = ApplicationService(env.storage.applications, True, 60)
    service.enqueue(env.vacancy_id, kind="not_now")
    assert env.storage.applications.next_due(utcnow()) is None
    assert env.storage.applications.next_due(utcnow() + timedelta(seconds=61)) is not None


def test_property_is_filled_only_when_empty(tmp_path):
    note = tmp_path / "n.md"
    note.write_text("---\na:\nb: x\nc:\n  - y\n---\ntext\n", encoding="utf-8")
    assert ObsidianVault.fill_property(note, "a", "1, 2")
    assert not ObsidianVault.fill_property(note, "b", "z") and not ObsidianVault.fill_property(note, "c", "z")
    assert frontmatter(note.read_text(encoding="utf-8")) == {"a": "1, 2", "b": "x", "c": ["y"]}


def test_names_and_links():
    assert note_file_name("Data Analyst / BI", "Citrus: iGaming") == "Citrus iGaming • Data Analyst BI"
    assert normalize_company("Linea Milanovic d.o.o.") == normalize_company("LINEA MILANOVIC") == "linea milanovic"
    assert insert_cv_link(LETTER, "https://drive/x").count("https://drive/x") == 1
    assert insert_cv_link("Резюме: [ВСТАВИТЬ]", "https://drive/x") == "Резюме: https://drive/x"
    assert "Резюме: [ВСТАВИТЬ]" in insert_cv_link(LETTER, None)
    assert employer_links(["https://www.linkedin.com/jobs/view/1", "https://citrus.careers-page.com/jobs/1?utm_x=1",
                           "https://indeed.com/1", "http://plain.example/1"]) == ["https://citrus.careers-page.com/jobs/1"]


def test_bad_llm_reply_is_an_error():
    with pytest.raises(ApplyError):
        MaterialsWriter.parse({"selection": {"lang": "ru"}, "letter": "x"})
    assert MaterialsWriter.parse({**REPLY, "skills": ["a", " ", "b"]}, max_skills=1).skills == ["a"]


def test_where_labels(env):
    builder = DossierBuilder(env.config, env.storage)
    facts = VacancyFacts.model_validate({"ad_language": "en", "work_format": "remote", "country": "Germany"})
    assert builder.where(facts) == ["🇺🇸: удаленка"]
    facts = VacancyFacts.model_validate({"ad_language": "en", "work_format": "office", "country": "Germany"})
    assert builder.where(facts) == [], "no such option in the user's list (28.10.2026)"
    facts = VacancyFacts.model_validate({"ad_language": "en", "work_format": "hybrid", "country": "Serbia"})
    assert builder.where(facts) == ["🇺🇸: в Сербии очно"]
    assert builder.where(VacancyFacts()) == []
    facts = VacancyFacts.model_validate({"ad_language": "ru", "work_format": "office", "country": "Russia"})
    assert builder.where(facts) == ["🇷🇺яз: очно в России"], "20.11.2026"
    facts = VacancyFacts.model_validate({"ad_language": "ru", "work_format": "hybrid", "country": "Kazakhstan"})
    assert builder.where(facts) == ["🇷🇺яз: очно в 3-ей стране"]


def test_apply_from_the_feed_and_sidebar_api(env):
    app = create_app(env.config, env.storage, port=PORT, applications=env.service)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        response = client.post(f"/api/vacancies/{env.vacancy_id}/status", json={"status": "applied"}, headers=ORIGIN)
        assert response.status_code == 200
        sections = client.get("/api/applications").json()["sections"]
        assert [s["key"] for s in sections] == ["purgatory"] and sections[0]["current"]
        [item] = sections[0]["items"]
        assert item["draft"] and item["state"] == "draft" and item["title"] == "Junior Data Scientist"
        assert run_all(env).state == ApplicationState.DRAFT and env.provider.calls == 0, "nothing until Generate"
        ok = client.post(f"/api/applications/{item['id']}/generate", json={"cv": True, "letter": True}, headers=ORIGIN)
        assert ok.status_code == 200
        assert client.post(f"/api/applications/{item['id']}/generate", json={}, headers=ORIGIN).status_code == 409
        run_all(env)
        sections = client.get("/api/applications").json()["sections"]
        assert [s["key"] for s in sections] == ["generated"]
        item = sections[0]["items"][0]
        assert item["status"] == "Applied" and item["has_pdf"] and item["has_letter"] and item["folder_url"]
        assert item["note_url"].startswith("obsidian://open?path=")
        assert "ВСТАВИТЬ" not in client.get(f"/api/applications/{item['id']}/letter").json()["text"]
        assert client.post(f"/api/applications/{item['id']}/dismiss", headers=ORIGIN).status_code == 404, "no ×"
        client.post(f"/api/vacancies/{env.vacancy_id}/status", json={"status": "new"}, headers=ORIGIN)
        assert env.storage.applications.for_vacancy(env.vacancy_id).state == ApplicationState.DONE
        assert client.get("/api/applications").json()["sections"] == []


@pytest.mark.parametrize("keep_note", [False, True])
def test_return_to_feed_then_generate_again(env, keep_note):
    app = create_app(env.config, env.storage, port=PORT, applications=env.service)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        status_url = f"/api/vacancies/{env.vacancy_id}/status"

        def status(value: str):
            return client.post(status_url, json={"status": value}, headers=ORIGIN)

        assert status("applied").status_code == 200
        first = env.storage.applications.for_vacancy(env.vacancy_id)
        generate_url = f"/api/applications/{first.id}/generate"
        assert client.post(generate_url, json={}, headers=ORIGIN).status_code == 200
        first = run_all(env)
        note = Path(first.note_path)
        if keep_note:
            note = note.rename(note.with_name("Renamed vacancy note.md"))
            note.write_text(f"---\nretriever_id: {env.vacancy_id}\ncustom: mine\n---\n"
                            "My old content\n\n### моё\nOld letter\n", encoding="utf-8")
        else:
            note.unlink()

        # Even abandoning the first replacement draft must keep the Drive identity of the previous run.
        for _ in range(2):
            assert status("new").status_code == 200
            assert client.get("/api/applications").json()["sections"] == []
            assert status("applied").status_code == 200
            [section] = client.get("/api/applications").json()["sections"]
            [draft] = section["items"]
            assert (section["key"], draft["id"], draft["vacancy_status"]) == ("purgatory", first.id, "applied")
            assert draft["draft"] and not draft["has_letter"] and not draft["has_pdf"] and not draft["note_url"]

        assert client.post(generate_url, json={}, headers=ORIGIN).status_code == 200
        second = run_all(env)
        assert second.state == ApplicationState.DONE and env.provider.calls == env.skill.builds == 2
        assert second.folder_url == folder_url("F2") and second.pdf_url == file_url("P2")
        assert Path(first.pdf_path).exists() and second.pdf_path != first.pdf_path
        assert second.note_path == str(note) and second.letter != first.letter
        new_note = note.read_text(encoding="utf-8")
        assert second.letter in new_note and "### описание вакансии" in new_note and second.warning is None
        assert "custom" not in frontmatter(new_note) and "My old content" not in new_note and "Old letter" not in new_note


def test_return_to_feed_while_worker_owns_the_application(env):
    app = create_app(env.config, env.storage, port=PORT, applications=env.service)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        url = f"/api/vacancies/{env.vacancy_id}/status"
        assert client.post(url, json={"status": "applied"}, headers=ORIGIN).status_code == 200
        queued = env.storage.applications.for_vacancy(env.vacancy_id)
        assert env.service.generate(queued.id, True, True)
        claimed = env.storage.applications.claim_due(utcnow())
        assert claimed and claimed.state == ApplicationState.WORKING
        assert client.post(url, json={"status": "new"}, headers=ORIGIN).status_code == 200
        assert client.get("/api/applications").json()["sections"] == []
        assert client.post(url, json={"status": "applied"}, headers=ORIGIN).status_code == 409
        assert env.storage.vacancies.get(env.vacancy_id).user_status == "new"
        assert env.pipeline.process(claimed).state == ApplicationState.CANCELLED
        assert client.post(url, json={"status": "applied"}, headers=ORIGIN).status_code == 200
        assert env.storage.applications.get(claimed.id).state == ApplicationState.DRAFT
        assert env.provider.calls == 0


def test_generate_without_cv_makes_the_note_only(env):
    app = apply_now(env, cv=False)
    app = run_all(env)
    assert app.state == ApplicationState.DONE and app.warning is None
    assert env.skill.builds == 0 and env.drive.folders == 0 and not app.letter and not app.folder_url
    assert [c["stage"] for c in env.storage.llm_calls.all()] == ["skills"], "one small call for the note's skills"
    text = Path(app.note_path).read_text(encoding="utf-8")
    meta = frontmatter(text)
    assert meta["👾статус"] == ["откликнулся"] and meta["👾необходимые скилы"] and meta["👾FK"] == "[[Acme (IT)]]"
    assert "### моё" not in text and "cover letter" not in text and "Google Drive" not in text
    assert "https://telegram.me/serbiawork/1" in text and "**описание вакансии:**" in text


def test_sidebar_sections(env):
    from vacancy_retriever.web.views import sidebar_sections

    env.storage.vacancies.set_user_status(env.vacancy_id, "applied")
    app = env.service.enqueue(env.vacancy_id)
    [section] = sidebar_sections(env.config, env.storage, env.service.visible())
    assert section["key"] == "purgatory"
    env.service.generate(app.id, True, True)
    run_all(env)
    keys = [s["key"] for s in sidebar_sections(env.config, env.storage, env.service.visible())]
    assert keys == ["generated"]


def test_obsidian_settings_defaults_match_the_vault_format():
    settings = ObsidianSettings()
    assert settings.tags[0] == "👾травля" and settings.what == "📋 вакансия"


def test_google_drive_needs_the_picked_folder(tmp_path):
    from vacancy_retriever.apply.drive import DriveParent, GoogleDrive, GoogleToken
    from vacancy_retriever.config import DriveSettings

    token = GoogleToken(tmp_path / "t.json", "1234-x.apps.googleusercontent.com", "s")
    token.save({"refresh_token": "r", "access_token": "a", "expires_at": time.time() + 3600})
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"files": []})
        return httpx.Response(200, json={"id": "NEW"})

    parent = DriveParent(tmp_path / "parent.json")
    drive = GoogleDrive(token, DriveSettings(), transport=httpx.MockTransport(handler), parent=parent,
                        pick_url="http://127.0.0.1:18765/drive/pick")
    with pytest.raises(ApplyError, match="drive/pick"):
        drive.ensure_folder(7, "🦆 X Y 28-10-2026")
    assert not [c for c in calls if c.method == "POST"], "nothing is created in My Drive without the pick"
    parent.save("TRAVLYA", "👾 травля")
    drive.ensure_folder(7, "🦆 X Y 28-10-2026")
    created = json.loads([c for c in calls if c.method == "POST"][0].content)
    assert created["parents"] == ["TRAVLYA"] and created["name"] == "🦆 X Y 28-10-2026"


def test_unknown_employer_is_never_called_unknown(tmp_path):
    from vacancy_retriever.apply.models import VacancyDossier
    from vacancy_retriever.apply.resume import ResumeSkill

    writer = MaterialsWriter(FakeProvider(), "sonnet", FakeSkill(), None)  # type: ignore[arg-type]
    dossier = VacancyDossier(vacancy_id=1, title="Data Analyst", employer="", text="text", links=[], where=[], labels=[])
    assert "unknown" not in writer.user_prompt(dossier).casefold()
    assert 'never write "Unknown"' in writer.system_prompt()

    skill_dir = tmp_path / "skill"
    (skill_dir / "scripts").mkdir(parents=True)
    # a stand-in build.py: prints the PDF line the way the real one does, with the empty company
    (skill_dir / "scripts" / "build.py").write_text(
        "import json, sys, pathlib\n"
        "sel = json.loads(pathlib.Path(sys.argv[2]).read_text())\n"
        "out = pathlib.Path(sys.argv[4]); stem = f\"Ismailov_CV_{sel['company']}_{sel['role_abbr']}\"\n"
        "(out / f'{stem}.pdf').write_bytes(b'%PDF'); (out / f'{stem}.tex').write_text('x')\n"
        "print(f'PDF: {out / (stem + \".pdf\")}  pages=1')\n", encoding="utf-8")
    tex = tmp_path / "bin"
    tex.mkdir()
    (tex / "tectonic").write_text("#!/bin/sh\n", encoding="utf-8")
    (tex / "tectonic").chmod(0o755)
    pdf, _ = ResumeSkill(skill_dir, tex).build({"lang": "en", "company": "Unknown", "role_abbr": "DA"}, tmp_path / "o")
    assert pdf.name == "Ismailov_CV_DA.pdf" and pdf.exists() and (pdf.parent / "Ismailov_CV_DA.tex").exists()


def test_grade_in_note_and_folder_names(env):
    builder = DossierBuilder(env.config, env.storage)

    def facts(**kw):
        return VacancyFacts.model_validate({"employer_type": "serbian", **kw})

    assert builder.name_title("Data Analyst", facts(seniority="junior")) == "Junior Data Analyst"
    assert builder.name_title("Data Analyst", facts(seniority="intern")) == "Intern Data Analyst"
    assert builder.name_title("Junior Data Analyst", facts(seniority="junior")) == "Junior Data Analyst"
    assert builder.name_title("ML Trainee", facts(seniority="intern")) == "ML Trainee"
    assert builder.name_title("Data Analyst", facts(seniority="middle")) == "Data Analyst"
    russian = facts(seniority="intern", employer_type="russian")
    assert builder.name_title("Аналитик данных", russian) == "Аналитик данных стажа"
    assert builder.name_title("Стажёр-аналитик", russian) == "Стажёр-аналитик"
    assert builder.name_title("Data Analyst", None) == "Data Analyst"


def test_drive_folder_name():
    from vacancy_retriever.apply.pipeline import drive_folder_name

    template = "{employer} • {title} • {date} 🦆"  # company first (user, 20.11.2026)
    assert drive_folder_name(template, "Data Analyst", "Tribe", date(2026, 10, 29)) == "Tribe • Data Analyst • 2026-10-29 🦆"
    assert drive_folder_name(template, "Data Analyst", "", date(2026, 10, 29)) == "Data Analyst • 2026-10-29 🦆"
    assert drive_folder_name(template, "BI / DWH", "X", date(2026, 10, 29)) == "X • BI ∕ DWH • 2026-10-29 🦆"
    old = "{title} • {employer} • {date} 🦆"  # a template with the employer in the middle still works
    assert drive_folder_name(old, "Data Analyst", "", date(2026, 10, 29)) == "Data Analyst • 2026-10-29 🦆"


def test_not_now_makes_a_note_with_skills_then_apply_turns_it_into_an_application(env):
    env.storage.vacancies.set_user_status(env.vacancy_id, "not_now")
    app = env.service.enqueue(env.vacancy_id, kind="not_now")
    app = run_all(env)
    assert app.state == ApplicationState.DONE and app.kind == "not_now"
    assert env.provider.calls == 1 and env.skill.builds == 0 and env.drive.folders == 0  # skills only
    text = Path(app.note_path).read_text(encoding="utf-8")
    meta = frontmatter(text)
    assert meta["👾статус"] == ["не сейчас"] and meta["👾FK"] is None and meta["👾необходимые скилы"]
    assert "### моё" not in text and "**описание вакансии:**" in text
    calls = env.storage.llm_calls.all()
    assert calls[-1]["stage"] == "skills" and calls[-1]["application_id"] == app.id
    assert app.stats["steps"].keys() >= {"note", "skills", "finalize"}
    [done] = env.storage.applications.visible()
    assert done.id == app.id, "a finished Not now stays in the sidebar, in Generated (20.11.2026)"
    item = application_item(env.config, env.storage, done)
    assert item["status"] == "Not now" and item["kind"] == "not_now"

    apply_now(env)  # ▶ later: the same note becomes the application
    app = run_all(env)
    assert app.kind == "apply" and app.state == ApplicationState.DONE and env.skill.builds == 1
    text = Path(app.note_path).read_text(encoding="utf-8")
    assert frontmatter(text)["👾статус"] == ["откликнулся"] and "### моё" in text and "**cover letter:**" in text
    assert len(list(env.vault.root.glob("*Junior*.md"))) == 1, "no second note"


def test_applied_then_not_now_stops_the_application(env):
    apply_now(env)
    app = run_all(env)
    env.storage.applications.update(app.id, state=ApplicationState.FAILED)
    env.service.enqueue(env.vacancy_id, kind="not_now")
    app = env.storage.applications.get(app.id)
    assert app.kind == "apply" and app.state == ApplicationState.CANCELLED


MANUAL_LETTER = "Добрый день!\n\nС детства увлекаюсь столярным делом\n\nБуду рад обсудить детали\n\nTelegram / WhatsApp: @ismylvdya"


def manual_env(env) -> tuple[list[str], int]:
    """The vacancy becomes hands-on work; the fake LLM answers like the non-IT letter call and keeps its prompts."""
    env.storage.db.execute("UPDATE vacancies SET category = 'manual_wood' WHERE id = ?", (env.vacancy_id,))
    systems: list[str] = []

    async def complete_json(system, user, model, schema=None):
        systems.append(system)
        return LlmResponse({"letter": MANUAL_LETTER, "skills": ["Сборка мебели"]}, LlmUsage(100, 50, model="m"), "")

    env.provider.complete_json = complete_json
    env.skill.letters = lambda: "===== references/letters.md =====\nNon-IT letters"
    return systems, env.vacancy_id


def test_hands_on_work_gets_a_note_with_a_letter_no_cv_and_no_drive(env):
    systems, vacancy_id = manual_env(env)
    apply_now(env, vacancy_id)
    app = run_all(env)
    assert app.state == ApplicationState.DONE and app.warning is None
    assert env.skill.builds == 0 and env.drive.folders == 0 and not app.pdf_path and not app.folder_url
    assert len(systems) == 1 and "Non-IT letters" in systems[0] and "ALWAYS in Russian" in systems[0]
    assert "CATALOG" not in systems[0] and "selection" not in systems[0]
    assert [c["stage"] for c in env.storage.llm_calls.all()] == ["letter"]
    text = Path(app.note_path).read_text(encoding="utf-8")
    meta = frontmatter(text)
    assert meta["👾лэйблы"] == ["📋не IT"] and meta["👾статус"] == ["откликнулся"]
    assert "https://telegram.me/serbiawork/1" in text and "**описание вакансии:**" in text
    assert f"### моё\n**cover letter:**\n```\n{MANUAL_LETTER}" in text
    assert "Google Drive" not in text and "ВСТАВИТЬ" not in text
    item = application_item(env.config, env.storage, app)
    assert item["has_letter"] and not item["has_pdf"] and not item["pdf_removed"]


def test_old_hands_on_application_with_a_cv_shows_no_pdf_buttons(env):
    apply_now(env)
    app = run_all(env)
    assert application_item(env.config, env.storage, app)["has_pdf"], "IT: the PDF button stays"
    manual_env(env)
    assert not application_item(env.config, env.storage, app)["has_pdf"]
    Path(app.pdf_path).unlink()
    assert not application_item(env.config, env.storage, app)["pdf_removed"], "no «Rebuild PDF» either"


def test_a_skill_copy_without_non_it_rules_stops_with_a_setup_error(tmp_path):
    skill_dir = tmp_path / "skill"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "references").mkdir()
    (skill_dir / "scripts" / "build.py").write_text("", encoding="utf-8")
    (skill_dir / "references" / "letters.md").write_text("## Short letters\n", encoding="utf-8")
    with pytest.raises(ApplyError) as error:
        ResumeSkill(skill_dir).letters()
    assert error.value.kind == "setup" and "Non-IT" in str(error.value)
    (skill_dir / "references" / "letters.md").write_text("## Non-IT letters\nrules", encoding="utf-8")
    assert "rules" in ResumeSkill(skill_dir).letters()


def test_real_config_points_to_a_skill_with_non_it_rules():
    """The app must not read the stale copy of the skill (28.10.2026, without the non-IT rules)."""
    assert "/skills/synced/" not in AppConfig().apply.resume.skill_dir


@pytest.mark.parametrize("title,expected", [
    ("Аналитик данных (Data Analyst)", "Data Analyst"),                       # РЖД, 18.11.2026
    ("Технический проджект менеджер/Technical project manager", "Technical project manager"),
    ("BI Developer/BI разработчик", "BI Developer"),
    ("Инженер по роботизации (RPA engineer)", "RPA engineer"),
    ("Менеджер продукта (Computer Vision)", "Менеджер продукта (Computer Vision)"),   # a domain, not the role
    ("Data Scientist (РСХБ-Экосистема)", "Data Scientist (РСХБ-Экосистема)"),
    ("Инженер-программист робототехнических комплексов (Node.js / TypeScript / Python)",
     "Инженер-программист робототехнических комплексов (Node.js / TypeScript / Python)"),
    ("Дизайнер / AI-креатор / ГК Марфа", "Дизайнер / AI-креатор / ГК Марфа"),
    ("Аналитик данных", "Аналитик данных"), ("Junior Data Analyst", "Junior Data Analyst"),
])
def test_note_and_folder_names_use_one_language(title, expected):
    assert english_title(title) == expected


def test_grade_goes_before_the_english_name(env):
    facts = VacancyFacts.model_validate({"title_original": "Аналитик данных (Data Analyst)", "seniority": "junior",
                                         "employer_type": "russian"})
    builder = DossierBuilder(env.config, env.storage)
    assert builder.name_title(english_title(facts.display_title), facts) == "Junior Data Analyst"


def test_sidebar_previous_days_start_at_three_in_the_morning(env, monkeypatch):
    """20.11.2026: «Previous days» = sent with «▶» before today's 03:00, local time of the laptop."""
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    from vacancy_retriever import timeutil
    from vacancy_retriever.web.views import sidebar_section

    belgrade = ZoneInfo("Europe/Belgrade")
    monkeypatch.setattr(timeutil, "machine_timezone", lambda: belgrade)
    apply_now(env)
    app = run_all(env)
    sent = dt(2026, 11, 19, 23, 30, tzinfo=belgrade)  # ▶ late in the evening
    env.storage.applications.db.execute("UPDATE applications SET created_at = ? WHERE id = ?",
                                        (timeutil.to_iso(sent), app.id))
    app = env.storage.applications.get(app.id)
    assert sidebar_section(env.config, app, dt(2026, 11, 20, 2, 59, tzinfo=belgrade)) == "generated", "before 03:00"
    assert sidebar_section(env.config, app, dt(2026, 11, 20, 3, 0, tzinfo=belgrade)) == "previous", "from 03:00"
    env.storage.applications.update(app.id, state=ApplicationState.DRAFT)
    draft = env.storage.applications.get(app.id)
    assert sidebar_section(env.config, draft, dt(2026, 11, 20, 1, 0, tzinfo=belgrade)) == "purgatory"
    assert sidebar_section(env.config, draft, dt(2026, 11, 21, 9, 0, tzinfo=belgrade)) == "previous"



def test_generate_the_cv_without_the_letter(env):
    systems = []
    real = env.provider.complete_json

    async def spy(system, user, model, schema=None):
        systems.append(system)
        return await real(system, user, model, schema)

    env.provider.complete_json = spy
    app = apply_now(env, cv=True, letter=False)
    app = run_all(env)
    assert app.state == ApplicationState.DONE and app.pdf_path and app.folder_url and not app.letter
    assert "ONLY for the tailored resume" in systems[0] and '"letter"' not in systems[0]
    text = Path(app.note_path).read_text(encoding="utf-8")
    assert f"### моё\n[папка на Google Drive]({folder_url('F1')})" in text
    assert "cover letter" not in text, "the folder, no letter section"


def test_the_letter_needs_the_cv_for_it_work(env):
    app = env.service.enqueue(env.vacancy_id)
    assert env.service.generate(app.id, False, True)
    app = env.storage.applications.get(app.id)
    assert not app.with_cv and not app.with_letter, "no CV — no letter (it carries the CV's link)"
    assert not env.service.generate(app.id, True, True), "only a row waiting in the purgatory"
