from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from ..config import AppConfig
from ..storage import Storage
from ..storage.applications import ApplicationRepository
from ..timeutil import utcnow
from .drive import CloudFolders, DriveParent, GoogleDrive, GoogleToken, NoDrive, StubDrive
from .models import Application, ApplicationState
from .obsidian import ObsidianVault
from .pipeline import ApplicationPipeline, ApplyServices, DossierBuilder
from .resume import MaterialsWriter, ResumeSkill

log = logging.getLogger(__name__)

TOKEN_FILE = "google_drive_token.json"


KEPT_FILES = {"note_mine.md"}  # tiny, and needed to keep the user's edits in «### моё»


def clean_cv_files(out_dir: Path, keep: set[int]) -> int:
    """PDF / .tex / selection.json of applications are deleted when the next Run starts (user, 28.10.2026: they stay
    in Finder until then, no CVs pile up in the project). Applications still in work (`keep`) are not touched.
    The selection stays in the database, so «Rebuild PDF» in the sidebar makes the PDF again without LLM."""
    if not out_dir.is_dir():
        return 0
    removed = 0
    for path in out_dir.glob("*/*"):
        if not path.parent.name.isdigit() or int(path.parent.name) in keep or path.name in KEPT_FILES:
            continue
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


class ApplicationWorker:
    """Background thread in the server: one application at a time (each claude CLI call is a separate process)."""

    def __init__(self, repo: ApplicationRepository, pipeline: ApplicationPipeline, poll_seconds: float = 15,
                 clock: Callable[[], datetime] = utcnow):
        self._repo = repo
        self._pipeline = pipeline
        self._poll = poll_seconds
        self._clock = clock
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        recovered = self._repo.recover()
        if recovered:
            log.info("Applications: %d interrupted by a restart, continuing", recovered)
        self._thread = threading.Thread(target=self._loop, name="apply-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def run_once(self) -> bool:
        app = self._repo.next_due(self._clock())
        if app is None:
            return False
        self._pipeline.process(app)
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.run_once():
                    continue
            except Exception:
                log.exception("Apply worker error")
            self._wake.wait(self._poll)
            self._wake.clear()


class ApplicationService:
    """What the UI may do with applications. ▶ = enqueue; Undo of ▶ = withdraw."""

    def __init__(self, repo: ApplicationRepository, enabled: bool, start_delay_seconds: int,
                 worker: ApplicationWorker | None = None, clock: Callable[[], datetime] = utcnow):
        self._repo = repo
        self.enabled = enabled
        self._delay = timedelta(seconds=start_delay_seconds)
        self._worker = worker
        self._clock = clock

    def start_worker(self) -> None:
        if self.enabled and self._worker:
            self._worker.start()

    def stop_worker(self) -> None:
        if self._worker:
            self._worker.stop()

    def _wake(self) -> None:
        if self._worker:
            self._worker.wake()

    def enqueue(self, vacancy_id: int) -> Application | None:
        if not self.enabled:
            return None
        app = self._repo.for_vacancy(vacancy_id)
        due = self._clock() + self._delay
        if app is None:
            app = self._repo.create(vacancy_id, due)
        elif app.state == ApplicationState.CANCELLED:
            self._repo.update(app.id, state=ApplicationState.QUEUED, due_at=due, dismissed=0, cancel_requested=0,
                              error=None)
        else:
            self._repo.update(app.id, dismissed=0, cancel_requested=0)
        self._wake()
        return self._repo.get(app.id)

    def withdraw(self, vacancy_id: int) -> None:
        """Undo of ▶: nothing done yet — forget it; otherwise stop (what is created stays, the row can be dismissed)."""
        app = self._repo.for_vacancy(vacancy_id)
        if app is None or app.state == ApplicationState.DONE:
            return
        if app.state == ApplicationState.QUEUED and not app.started:
            self._repo.delete(app.id)
        elif app.state == ApplicationState.WORKING:
            self._repo.update(app.id, cancel_requested=1)
        else:
            self._repo.update(app.id, state=ApplicationState.CANCELLED)

    def retry(self, application_id: int) -> bool:
        app = self._repo.get(application_id)
        if app is None or app.state == ApplicationState.WORKING:
            return False
        self._repo.update(app.id, state=ApplicationState.QUEUED, due_at=self._clock(), error=None, cancel_requested=0)
        self._wake()
        return True

    def dismiss(self, application_id: int) -> bool:
        app = self._repo.get(application_id)
        if app is None:
            return False
        self._repo.update(app.id, dismissed=1)
        return True

    def get(self, application_id: int) -> Application | None:
        return self._repo.get(application_id)

    def visible(self) -> list[Application]:
        return self._repo.visible()


def reveal_in_finder(path: Path) -> bool:
    if not path.exists():
        return False
    subprocess.run(["open", "-R", str(path)], check=False)
    return True


PARENT_FILE = "google_drive_parent.json"


def create_drive(config: AppConfig, pick_url: str = "http://127.0.0.1:18765/drive/pick") -> CloudFolders:
    settings = config.apply.drive
    if settings.stub:
        return StubDrive()
    if not settings.enabled:
        return NoDrive("Google Drive is off (config/apply.yaml → drive.enabled)")
    client_id, secret = config.secrets.get("GOOGLE_OAUTH_CLIENT_ID"), config.secrets.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not secret:
        return NoDrive("Google Drive not connected: GOOGLE_OAUTH_CLIENT_ID / _SECRET missing in config/.env")
    token = GoogleToken(config.paths.data / TOKEN_FILE, client_id, secret)
    if not token.exists:
        return NoDrive("Google Drive not connected: run `vacancy-retriever drive-login`")
    return GoogleDrive(token, settings, parent=DriveParent(config.paths.data / PARENT_FILE), pick_url=pick_url)


def create_application_service(config: AppConfig, storage: Storage) -> ApplicationService:
    """Wires the real Obsidian vault, Google Drive, the resume-tailor skill and the LLM of llm.yaml."""
    from ..llm.factory import create_provider

    apply = config.apply
    vault_dir = config.resolve_path(apply.obsidian.vault_dir) if apply.obsidian.vault_dir else None
    vault = ObsidianVault(vault_dir, apply.obsidian) if vault_dir and vault_dir.is_dir() else None
    resume = apply.resume
    skill = ResumeSkill(config.resolve_path(resume.skill_dir),
                        config.resolve_path(resume.tex_bin_dir) if resume.tex_bin_dir else None,
                        config.paths.root / ".cache" / "tectonic")
    llm = config.llm
    sdk = llm.claude_sdk.model_copy(update={"effort": resume.effort, "thinking": None})
    model = llm.stage_models.resume or llm.stage_models.scoring
    writer = MaterialsWriter(create_provider(config, sdk), model, skill, storage.llm_calls, resume.max_skills)
    services = ApplyServices(DossierBuilder(config, storage), vault, create_drive(config), writer, skill,
                             config.paths.data / "applications", apply.drive.folder_name)
    pipeline = ApplicationPipeline(storage, services, apply.retry_minutes)
    worker = ApplicationWorker(storage.applications, pipeline)
    return ApplicationService(storage.applications, apply.enabled, apply.start_delay_seconds, worker)
