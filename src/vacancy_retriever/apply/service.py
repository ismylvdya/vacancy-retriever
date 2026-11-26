from __future__ import annotations

import logging
import os
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
from .models import Application, ApplicationState, ApplyError
from .obsidian import ObsidianVault
from .pipeline import ApplicationPipeline, ApplyServices, DossierBuilder
from .resume import MaterialsWriter, ResumeSkill

log = logging.getLogger(__name__)

TOKEN_FILE = "google_drive_token.json"


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
        app = self._repo.claim_due(self._clock())
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
    """What the UI may do with applications. ▶ = enqueue (purgatory), Generate = generate; Undo of ▶ = withdraw."""

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

    def wake_worker(self) -> None:
        if self._worker:
            self._worker.wake()

    def enqueue(self, vacancy_id: int, kind: str = "apply", *, fresh: bool = False) -> Application | None:
        """▶ (kind apply) puts the vacancy into the sidebar's purgatory: nothing is made until Generate (user,
        18.11.2026). N (kind not_now: the Obsidian note with the skills) starts by itself after the Undo window.
        A «не сейчас» note, then ▶: the same application becomes an apply and waits for Generate (the note is reused)."""
        if not self.enabled:
            return None
        app = self._repo.for_vacancy(vacancy_id)
        due = self._clock() + self._delay
        state = ApplicationState.DRAFT if kind == "apply" else ApplicationState.QUEUED
        if app is None:
            app = self._repo.create(vacancy_id, due, kind, state)
        elif fresh:
            if app.state == ApplicationState.WORKING:
                raise ApplyError("The previous attempt is still stopping. Try again after the current step finishes.")
            self._repo.restart(app.id, due, kind, state)
        elif app.kind != kind:
            if kind == "not_now" and app.started:  # applied earlier, now Not now: the apply stops, its files stay
                self.stop(app.id)
                return self._repo.get(app.id)
            self._repo.update(app.id, kind=kind, state=state, due_at=due, dismissed=0, cancel_requested=0, error=None)
        elif app.state == ApplicationState.CANCELLED:
            self._repo.update(app.id, state=state, due_at=due, dismissed=0, cancel_requested=0, error=None)
        else:
            self._repo.update(app.id, dismissed=0, cancel_requested=0)
        self.wake_worker()
        return self._repo.get(app.id)

    def generate(self, application_id: int, with_cv: bool, with_letter: bool, manual: bool = False) -> bool:
        """«Generate Obsidian note» in the purgatory: the note always, plus the CV (+ Drive folder) and / or the cover
        letter if checked. For IT the letter comes only with the CV (it carries the CV's link); hands-on work has a
        letter and no CV (user, 19.11.2026)."""
        if manual:
            with_cv = False
        elif not with_cv:
            with_letter = False
        with self._repo.db.transaction():
            app = self._repo.get(application_id)
            if app is None or app.dismissed or app.kind != "apply" or app.state != ApplicationState.DRAFT:
                return False
            self._repo.update(app.id, state=ApplicationState.QUEUED, due_at=self._clock(), with_cv=int(with_cv),
                              with_letter=int(with_letter), error=None, cancel_requested=0)
        self.wake_worker()
        return True

    def withdraw(self, vacancy_id: int, *, hide: bool = False) -> None:
        """Stop the current attempt, hiding it when the vacancy returns to the Feed."""
        app = self._repo.for_vacancy(vacancy_id)
        if app is None:
            return
        if app.state == ApplicationState.DRAFT and not app.started and app.generation == 1:
            self._repo.delete(app.id)
        elif app.state == ApplicationState.DRAFT:
            self._repo.update(app.id, state=ApplicationState.CANCELLED)
        else:
            self.stop(app.id)
        if hide:
            self._repo.update(app.id, dismissed=1)

    def stop(self, application_id: int) -> bool:
        """Stop button / Undo. A step in progress (a Claude call) finishes first, then nothing more is done."""
        return self._repo.stop(application_id)

    def retry(self, application_id: int) -> bool:
        with self._repo.db.transaction():
            app = self._repo.get(application_id)
            if app is None or app.dismissed or app.state in (ApplicationState.WORKING, ApplicationState.DRAFT):
                return False
            self._repo.update(app.id, state=ApplicationState.QUEUED, due_at=self._clock(), error=None, cancel_requested=0)
        self.wake_worker()
        return True

    def get(self, application_id: int) -> Application | None:
        return self._repo.get(application_id)

    def visible(self) -> list[Application]:
        return self._repo.visible()


def reveal_in_finder(path: Path) -> bool:
    if not path.exists():
        return False
    if subprocess.run(["open", "-R", str(path)], check=False).returncode != 0:
        return False
    try:
        subprocess.run(["pbcopy"], input=path.name.encode("utf-8"), check=True,
                       env={**os.environ, "LC_ALL": "en_US.UTF-8"})
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ApplyError("Finder opened, but the PDF filename could not be copied") from exc
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
    writer = MaterialsWriter(create_provider(config, sdk), model, skill, storage.llm_calls, resume.max_skills,
                             skills_model=llm.stage_models.scoring)
    services = ApplyServices(DossierBuilder(config, storage), vault, create_drive(config), writer, skill,
                             config.resolve_path(resume.output_dir), apply.drive.folder_name)
    pipeline = ApplicationPipeline(storage, services, apply.retry_minutes)
    worker = ApplicationWorker(storage.applications, pipeline)
    return ApplicationService(storage.applications, apply.enabled, apply.start_delay_seconds, worker)
