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
from vacancy_retriever.apply.pipeline import ApplicationPipeline, ApplyServices, DossierBuilder
from vacancy_retriever.apply.resume import MaterialsWriter, insert_cv_link
from vacancy_retriever.apply.service import clean_cv_files, ApplicationService, ApplicationWorker
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
        assert "SKILL" in system and "CATALOG" in system and "Junior Data Scientist" in user
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

    @property
    def available(self) -> bool:
        return True

    def ensure_folder(self, key: int, name: str) -> DriveItem:
        self.folders += 1
        self.names = [*getattr(self, "names", []), name]
        return DriveItem("F1", folder_url("F1"))

    def upload_pdf(self, key: int, folder_id: str, path: Path) -> DriveItem:
        self.uploads += 1
        return DriveItem("P1", file_url("P1"))


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


def test_full_pipeline_makes_note_cv_letter_and_folder(env):
    env.service.enqueue(env.vacancy_id)
    app = run_all(env)
    assert app.state == ApplicationState.DONE and app.error is None and app.warning is None
    assert app.letter.endswith(f"Резюме: {file_url('P1')}\nGitHub: https://github.com/x")
    assert Path(app.pdf_path).exists() and app.folder_url == folder_url("F1")
    note = Path(app.note_path)
    assert note.name == "Junior Data Scientist - Acme d.o.o..md"
    text = note.read_text(encoding="utf-8")
    meta = frontmatter(text)
    assert meta["tags"] == ["👾травля"] and meta["👾что_это"] == ["📋 вакансия"]
    assert meta["👾лэйблы"] == ["🦆 retrieved"], "made by the app: a label, not a tag"
    assert meta["👾где"] == ["🇷🇺яз: в Сербии очно"] and meta["retriever_id"] == env.vacancy_id
    assert meta["👾FK"] == "[[Acme (IT)]]", "employer note found by its name without the legal suffix"
    assert meta["👾необходимые скилы"] == "Python, SQL, A/B testing"
    assert meta["👾статус"] == ["откликнулся"] and str(meta["начало"]) == date.today().isoformat()
    assert "%%" not in text, "no service markers in the note"
    assert text.index("### описание вакансии") < text.index("https://telegram.me/serbiawork/1") < text.index("### моё")
    assert "https://telegram.me/serbiawork/1" in text and "Полный текст вакансии про Data Science" in text
    assert f"### моё\n[папка Google Drive]({folder_url('F1')})\n\n**cover letter:**\n```" in text
    assert file_url("P1") in text
    assert "### описание вакансии\n[эта вакансия в Telegram-канале " in text
    assert "](https://telegram.me/serbiawork/1)" in text and "\n\n**описание вакансии:**\n```" in text
    assert "ВСТАВИТЬ" not in text
    assert env.drive.names == [f"🦆 Junior Data Scientist - Acme d.o.o. {date.today():%d-%m-%Y}"]
    calls = env.storage.llm_calls.all()
    assert [c["stage"] for c in calls] == ["resume"] and calls[0]["input_tokens"] == 1000


def test_claude_limit_waits_then_resumes_without_redoing_steps(env):
    env.provider.fail = LlmError("usage limit reached", kind="limit", retryable=False)
    env.service.enqueue(env.vacancy_id)
    app = run_all(env)
    assert app.state == ApplicationState.WAITING and app.step == "materials"
    assert app.due_at > utcnow() + timedelta(minutes=29) and app.note_path and app.folder_id
    env.provider.fail = None
    assert env.service.retry(app.id)
    app = run_all(env)
    assert app.state == ApplicationState.DONE
    assert env.drive.folders == 1 and env.provider.calls == 2 and env.skill.builds == 1
    assert len(list(env.vault.root.glob("Junior*.md"))) == 1, "the note is not created twice"


def test_restart_continues_interrupted_work(env):
    app = env.service.enqueue(env.vacancy_id)
    env.storage.applications.update(app.id, state=ApplicationState.WORKING, step="pdf",
                                    materials_json=Materials(REPLY["selection"], LETTER, ["Python"]).to_json())
    assert env.storage.applications.recover() == 1
    app = run_all(env)
    assert app.state == ApplicationState.DONE and env.provider.calls == 0, "the LLM result was already stored"


def test_a_taken_note_name_gets_a_number(env):
    taken = env.vault.root / "Junior Data Scientist - Acme d.o.o..md"
    taken.write_text("my own note", encoding="utf-8")
    env.service.enqueue(env.vacancy_id)
    app = run_all(env)
    assert Path(app.note_path).name == "Junior Data Scientist - Acme d.o.o. 2.md"
    assert taken.read_text(encoding="utf-8") == "my own note"


def test_user_edits_in_the_note_are_kept(env):
    env.service.enqueue(env.vacancy_id)
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


def test_cv_files_are_removed_at_the_next_run_and_rebuilt_without_llm(env):
    env.service.enqueue(env.vacancy_id)
    app = run_all(env)
    pdf = Path(app.pdf_path)
    (pdf.parent / "note_mine.md").write_text("kept", encoding="utf-8")
    assert clean_cv_files(pdf.parent.parent, keep={app.vacancy_id}) == 0, "an application in work is not touched"
    assert clean_cv_files(pdf.parent.parent, keep=set()) >= 1
    assert not pdf.exists() and (pdf.parent / "note_mine.md").exists()
    env.service.retry(app.id)
    app = run_all(env)
    assert Path(app.pdf_path).exists() and env.provider.calls == 1 and env.skill.builds == 2


def test_mine_section_follows_the_app_until_the_user_edits_it(env):
    env.services.drive = NoDrive("Google Drive not connected")
    env.service.enqueue(env.vacancy_id)
    app = run_all(env)
    note = Path(app.note_path)
    assert "Google Drive не подключён" in note.read_text(encoding="utf-8")
    env.services.drive = env.drive  # Drive connected later: Retry puts the real links in
    env.service.retry(app.id)
    app = run_all(env)
    text = note.read_text(encoding="utf-8")
    assert f"[папка Google Drive]({folder_url('F1')})" in text and file_url("P1") in text
    assert "Google Drive не подключён" not in text and text.count("### моё") == 1


def test_without_drive_the_rest_is_done_and_letter_keeps_the_marker(env):
    env.services.drive = NoDrive("Google Drive not connected")
    env.service.enqueue(env.vacancy_id)
    app = run_all(env)
    assert app.state == ApplicationState.DONE and app.warning == "Google Drive not connected"
    assert "ВСТАВИТЬ" in app.letter and app.folder_url is None
    assert "Google Drive не подключён" in Path(app.note_path).read_text(encoding="utf-8")


def test_drive_stub_gives_obviously_fake_links_and_a_warning(env):
    env.services.drive = StubDrive()
    env.service.enqueue(env.vacancy_id)
    app = run_all(env)
    assert app.state == ApplicationState.DONE and "stub" in app.warning
    assert app.folder_url.startswith("https://drive-stub.invalid/") and "ВСТАВИТЬ" not in app.letter
    assert "https://drive-stub.invalid/" in app.letter and "drive.google.com" not in app.letter


def test_setup_error_fails_with_retry(env):
    def broken(selection, out_dir):
        raise ApplyError("xelatex not found", kind="setup")

    env.skill.build = broken
    env.service.enqueue(env.vacancy_id)
    app = run_all(env)
    assert app.state == ApplicationState.FAILED and app.step == "pdf" and "xelatex" in app.error


def test_undo_before_start_forgets_after_start_cancels(env):
    env.service.enqueue(env.vacancy_id)
    env.service.withdraw(env.vacancy_id)
    assert env.storage.applications.for_vacancy(env.vacancy_id) is None
    app = env.service.enqueue(env.vacancy_id)
    env.storage.applications.update(app.id, state=ApplicationState.FAILED, note_path="/x.md")
    env.service.withdraw(env.vacancy_id)
    assert env.storage.applications.get(app.id).state == ApplicationState.CANCELLED
    env.service.enqueue(env.vacancy_id)
    assert env.storage.applications.get(app.id).state == ApplicationState.QUEUED


def test_start_delay_gives_time_for_undo(env):
    service = ApplicationService(env.storage.applications, True, 60)
    service.enqueue(env.vacancy_id)
    assert env.storage.applications.next_due(utcnow()) is None
    assert env.storage.applications.next_due(utcnow() + timedelta(seconds=61)) is not None


def test_property_is_filled_only_when_empty(tmp_path):
    note = tmp_path / "n.md"
    note.write_text("---\na:\nb: x\nc:\n  - y\n---\ntext\n", encoding="utf-8")
    assert ObsidianVault.fill_property(note, "a", "1, 2")
    assert not ObsidianVault.fill_property(note, "b", "z") and not ObsidianVault.fill_property(note, "c", "z")
    assert frontmatter(note.read_text(encoding="utf-8")) == {"a": "1, 2", "b": "x", "c": ["y"]}


def test_names_and_links():
    assert note_file_name("Data Analyst / BI", "Citrus: iGaming") == "Data Analyst BI - Citrus iGaming"
    assert normalize_company("Linea Milanovic d.o.o.") == normalize_company("LINEA MILANOVIC") == "linea milanovic"
    assert insert_cv_link(LETTER, "https://drive/x").count("https://drive/x") == 1
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


def test_apply_from_the_feed_and_sidebar_api(env):
    app = create_app(env.config, env.storage, port=PORT, applications=env.service)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        response = client.post(f"/api/vacancies/{env.vacancy_id}/status", json={"status": "applied"}, headers=ORIGIN)
        assert response.status_code == 200
        items = client.get("/api/applications").json()["items"]
        assert [i["state"] for i in items] == ["queued"] and items[0]["title"] == "Junior Data Scientist"
        assert client.get(f"/api/applications/{items[0]['id']}/letter").status_code == 404
        run_all(env)
        item = client.get("/api/applications").json()["items"][0]
        assert item["status"] == "Ready" and item["has_pdf"] and item["has_letter"] and item["folder_url"]
        assert item["note_url"].startswith("obsidian://open?path=")
        assert "ВСТАВИТЬ" not in client.get(f"/api/applications/{item['id']}/letter").json()["text"]
        assert client.post(f"/api/applications/{item['id']}/dismiss", headers=ORIGIN).status_code == 200
        assert client.get("/api/applications").json()["items"] == []
        client.post(f"/api/vacancies/{env.vacancy_id}/status", json={"status": "new"}, headers=ORIGIN)
        assert env.storage.applications.for_vacancy(env.vacancy_id).state == ApplicationState.DONE


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
