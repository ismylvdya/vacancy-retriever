from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from urllib.parse import urlencode
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ..app_info import APP_NAME, APP_VERSION
from ..apply.drive import DriveParent, GoogleDrive
from ..apply.manual import ManualCvPicker, ManualError, ManualIntake
from ..apply.models import ApplyError
from ..apply.service import PARENT_FILE, ApplicationService, create_application_service, create_drive, reveal_in_finder
from ..config import Secrets, AppConfig, SourceConfig
from .. import notices
from ..icons import IconStore
from ..pipeline.manager import AlreadyRunning, RunManager
from ..pipeline.recalculate import recalculate
from ..pipeline.runner import RunOptions
from ..settings.service import LinkedInSwitchError, SettingsError, SettingsService
from ..storage import Storage
from ..timeutil import format_local, from_iso, local, utcnow
from . import views

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent
STATIC_ICONS = WEB_DIR / "static" / "icons"


class StatusIds(BaseModel):
    ids: list[int]


class StatusChange(BaseModel):
    status: str


class LinkedInPagesOn(BaseModel):
    confirm: str = ""


class SettingsChanges(BaseModel):
    changes: dict[str, Any]


class Report(BaseModel):
    text: str


class GenerateChoice(BaseModel):
    """Purgatory → «Generate Obsidian note»: the note always, the CV and the letter if checked."""

    cv: bool = True      # the CV (+ the Drive folder)
    letter: bool = True  # the cover letter (IT: only together with the CV)


class ManualVacancyInput(BaseModel):
    """«Add vacancy» window: a vacancy the user found himself (apply/manual.py)."""

    title: str
    url: str
    description: str
    company: str = ""  # optional: the note's and the folder's names carry it
    cv: bool = True
    letter: bool = True
    pdf_token: str | None = None
    cover_letter: str | None = None


class DriveChoice(BaseModel):
    id: str


class RunRequest(BaseModel):
    """Run panel: days = how far back (None = since the last run); groups = telegram / sites / email (None = all)."""

    days: int | None = None
    groups: list[str] | None = None
    vacancy_groups: list[int] | None = None  # feed groups of this run (None = every group that is on)
    include_missed: bool = False  # also collect the missed periods of the chosen sources (banner in the panel)


SOURCE_GROUPS = {"telegram": lambda s: s.type == "telegram", "email": lambda s: s.type == "email",
                 "sites": lambda s: s.type not in ("telegram", "email")}


def describe_status(state: dict[str, Any], order: list[int] | None = None) -> dict[str, Any]:
    """Adds the header texts: 'Last run: 13 Oct 08:12 (14 min)' and the run summary line."""
    last = state.get("last_run")
    state["last_run_line"] = "No runs yet"
    state["last_run_short"] = "No runs yet"
    state["summary_line"] = ""
    if last:
        started, finished = from_iso(last["started_at"]), from_iso(last["finished_at"])
        minutes = round((finished - started).total_seconds() / 60) if started and finished else 0
        state["last_run_line"] = f"Last run: {format_local(started)} ({minutes} min)"
        same_day = local(started) and local(started).date() == local(utcnow()).date()
        state["last_run_short"] = f"Updated {format_local(started, '%H:%M' if same_day else '%-d %b %H:%M')}"
        summary = last.get("summary") or {}
        if last["status"] == "failed":
            state["summary_line"] = f"Run {format_local(started)} failed"
        else:
            by_group = summary.get("new_by_group") or {}
            groups = " / ".join(str(by_group.get(str(g), 0)) for g in order or sorted(int(k) for k in by_group))
            line = f"Run {format_local(started)} · new {summary.get('new_total', 0)} ({groups})"
            if summary.get("errors"):
                line += f" · errors {summary['errors']}"
            state["summary_line"] = line
    return state


def _static_version() -> str:
    """Changes whenever a static file changes, so browsers never keep an old app.js / style.css."""
    digest = hashlib.sha1(APP_VERSION.encode())
    for path in sorted((WEB_DIR / "static").glob("*.*")):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def _allowed_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}"}


def create_app(config: AppConfig, storage: Storage, port: int, manager: RunManager | None = None,
               applications: ApplicationService | None = None) -> FastAPI:
    app = FastAPI(title=APP_NAME, version=APP_VERSION, docs_url=None, redoc_url=None, openapi_url=None)
    manager = manager or RunManager(config, storage)
    applications = applications or create_application_service(config, storage)
    manual_cv = ManualCvPicker()
    app.state.applications = applications
    manager.recover()
    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    templates.env.globals.update(app_name=APP_NAME, version=_static_version())
    icons = IconStore(config.paths)
    allowed_hosts = _allowed_hosts(port)
    allowed_origins = {f"http://{host}" for host in allowed_hosts}

    @app.middleware("http")
    async def local_only(request: Request, call_next: Any) -> Response:
        if request.headers.get("host") not in allowed_hosts:
            return JSONResponse({"detail": "Forbidden host"}, status_code=403)
        if request.method not in ("GET", "HEAD") and request.headers.get("origin") not in allowed_origins:
            return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
        return await call_next(request)

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    def full_status() -> dict[str, Any]:
        return describe_status(manager.status(), config.rules.enabled_groups)

    def page(request: Request, template: str, tab: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, template, {
            "tab": tab, "feed_count": storage.vacancies.feed_count(), "status": full_status(),
            "statuses_new": storage.statuses.new_count(),
            "health_problems": health_problems(), "notices": notices.active(config.paths.notices),
            "max_days": config.rules.max_age_days, "missed": missed_summary(),
            "run_groups": [{"number": n, "title": config.rules.groups[n].title} for n in config.rules.enabled_groups],
            **context,
        })

    def missed_summary() -> dict[str, Any] | None:
        """Run panel banner: the span of all missed periods and the names of their sources."""
        periods = storage.runs.missed()
        if not periods:
            return None
        names = {s.id: s.name for s in config.sources.sources}
        start, end = min(a for a, _ in periods.values()), max(b for _, b in periods.values())
        groups = sorted({g for s in config.sources.sources if s.id in periods
                         for g, test in SOURCE_GROUPS.items() if test(s)})
        return {"from": format_local(start, "%-d %b %H:%M"), "to": format_local(end, "%-d %b %H:%M"),
                "count": len(periods), "groups": groups,
                "sources": ", ".join(sorted(names.get(s, s) for s in periods))}

    def health_problems() -> int:
        return sum(1 for row in views.latest_source_states(views.health(config, storage, limit=6)) if row.warn)

    def search_params(request: Request) -> tuple[str, str]:
        search = request.query_params.get("q", "").strip()
        where = request.query_params.get("in", "all")
        return search, where if where in dict(views.SEARCH_FIELDS) else "all"

    @app.get("/", response_class=HTMLResponse)
    def feed(request: Request, stars: str | None = None) -> HTMLResponse:
        """Filters like in Processed, plus the minimum fit (★5 / ★4+ … / all; ★4+ by default, user 28.10.2026)."""
        min_stars = views.FEED_MIN_STARS_DEFAULT if stars is None else (int(stars) if stars.isdigit() else 0)
        chosen: dict[str, set[str]] = {}
        for key, value in request.query_params.multi_items():
            if key not in ("stars", "q", "in") and value:
                chosen.setdefault(key, set()).add(value)
        search, where = search_params(request)
        records = storage.vacancies.feed(config.rules.feed_category_order)
        above = [r for r in records if (r.stars or 0) >= min_stars]
        matched = views.search_records(storage, above, search, where)
        kept, facets = views.apply_filters(config, matched, chosen, skip={"reason", "fit"})
        cards = views.CardBuilder(config, storage)  # per request: latest run, reports and rates change over time
        built = [cards.build(record) for record in kept]
        return page(request, "feed.html", "feed", groups=views.group_feed(built, config), facets=facets,
                    min_stars=min_stars, star_options=views.star_options(records, min_stars),
                    active=bool(chosen or search) or min_stars != views.FEED_MIN_STARS_DEFAULT,
                    search=search, where=where, search_fields=views.SEARCH_FIELDS,
                    shown=len(built), all_count=len(records))

    processed_tabs = [("all", "All manually"), ("applied", "Applied"), ("not_now", "Not now"), ("skipped", "Skipped")]

    @app.get("/processed", response_class=HTMLResponse)
    def processed(request: Request, filter: str | None = None) -> HTMLResponse:
        if filter == "hidden":
            hidden = views.hidden_items(config, storage)
            return page(request, "processed.html", "processed", runs=[], hidden=hidden, total=0, current="hidden",
                        counts=storage.vacancies.processed_counts(), filters=processed_tabs, facets=[],
                        active=False, query="", search="", where="all", search_fields=[], reported_count=storage.vacancies.reports_count())
        chosen: dict[str, set[str]] = {}
        for key, value in request.query_params.multi_items():
            if key not in ("filter", "q", "in") and value:
                chosen.setdefault(key, set()).add(value)
        search, where = search_params(request)
        records = views.search_records(storage, storage.vacancies.processed(filter, limit=10000), search, where)
        kept, filters = views.apply_filters(config, records, chosen)
        cards = views.CardBuilder(config, storage)
        built = [cards.build(record) for record in kept]
        pairs = [(k, v) for k, values in sorted(chosen.items()) for v in sorted(values)]
        query = urlencode(pairs + ([("q", search)] + ([("in", where)] if where != "all" else []) if search else []))
        # What the user did is grouped by the day they did it; what the app did by the run that did it.
        grouped = (views.group_by_run(built, storage) if filter == "auto_skipped"
                   else views.group_by_action_day(built, filter))
        return page(request, "processed.html", "processed", runs=grouped,
                    total=len(built), current=filter or "all", counts=storage.vacancies.processed_counts(),
                    filters=processed_tabs, facets=filters, active=bool(chosen or search), query=query,
                    search=search, where=where, search_fields=views.SEARCH_FIELDS,
                    reported_count=storage.vacancies.reports_count())

    @app.get("/statuses", response_class=HTMLResponse)
    def statuses(request: Request) -> HTMLResponse:
        """Statuses of applications found in e-mails (rejections, invitations, tests…), not linked to vacancies."""
        return page(request, "statuses.html", "statuses", sections=views.status_sections(config, storage))

    @app.post("/api/statuses/seen")
    def statuses_seen(body: StatusIds) -> dict[str, Any]:
        """The Statuses tab was opened with these rows: from now on they are «Seen» (they stay highlighted until the
        page is reloaded)."""
        return {"marked": storage.statuses.mark_seen(body.ids), "new": storage.statuses.new_count()}

    @app.post("/api/statuses/{status_id}/unseen")
    def status_unseen(status_id: int) -> dict[str, Any]:
        if not storage.statuses.mark_unseen(status_id):
            raise HTTPException(404, "Status not found")
        return {"new": storage.statuses.new_count()}

    @app.get("/api/vacancies/{vacancy_id}/debug")
    def vacancy_debug(vacancy_id: int) -> dict[str, Any]:
        info = views.debug_info(config, storage, vacancy_id)
        if info is None:
            raise HTTPException(404, "Vacancy not found")
        return info

    @app.get("/statistics", response_class=HTMLResponse)
    def statistics(request: Request, view: str = "sources") -> HTMLResponse:
        runs = views.health(config, storage)
        view = view if view in ("sources", "runs", "applies") else "sources"
        return page(request, "statistics.html", "statistics", view=view, runs=runs,
                    latest=views.latest_source_states(runs), data=views.tokens(config, storage),
                    linkedin_all_time=views.linkedin_all_time(storage),
                    applies=views.apply_stats(config, storage) if view == "applies" else [],
                    applies_count=len(storage.applications.history()))

    @app.get("/health")
    def health() -> RedirectResponse:
        return RedirectResponse("/statistics", status_code=301)

    @app.get("/tokens")
    def tokens() -> RedirectResponse:
        return RedirectResponse("/statistics?view=runs", status_code=301)

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        state = full_status()
        state["feed_count"] = storage.vacancies.feed_count()
        return state

    @app.post("/api/run")
    def start_run(request: RunRequest | None = None) -> dict[str, Any]:
        request = request or RunRequest()
        if request.days is not None and not 1 <= request.days <= config.rules.max_age_days:
            raise HTTPException(400, f"Days must be 1–{config.rules.max_age_days}")
        source_ids = None
        if request.groups is not None:
            unknown = set(request.groups) - SOURCE_GROUPS.keys()
            if unknown or not request.groups:
                raise HTTPException(400, "Choose at least one of: telegram, sites, email")
            source_ids = [s.id for s in config.sources.sources if s.enabled
                          and any(SOURCE_GROUPS[g](s) for g in request.groups)]
        vacancy_groups = None
        if request.vacancy_groups is not None:
            enabled = set(config.rules.enabled_groups)
            if not request.vacancy_groups or not set(request.vacancy_groups) <= enabled:
                raise HTTPException(400, "Choose at least one feed group that is on in Settings")
            vacancy_groups = sorted(set(request.vacancy_groups))
        since = timedelta(days=request.days) if request.days else None
        backfill = None
        if request.include_missed:
            chosen = set(source_ids) if source_ids is not None else {s.id for s in config.sources.sources if s.enabled}
            backfill = {s: start for s, (start, _) in storage.runs.missed().items() if s in chosen} or None
        try:
            manager.start(RunOptions(source_ids=source_ids, vacancy_groups=vacancy_groups, since=since,
                                     backfill=backfill, trigger="ui"))
        except AlreadyRunning as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"started": True}

    @app.post("/api/missed/dismiss")
    def dismiss_missed() -> dict[str, Any]:
        storage.runs.clear_missed()
        return {"ok": True}

    @app.post("/api/cancel")
    def cancel_run() -> dict[str, Any]:
        return {"cancelled": manager.cancel()}

    @app.post("/api/vacancies/{vacancy_id}/status")
    def change_status(vacancy_id: int, change: StatusChange) -> dict[str, Any]:
        try:
            with storage.db.transaction():
                previous = storage.vacancies.get(vacancy_id)
                if not previous or not storage.vacancies.set_user_status(vacancy_id, change.status):
                    raise HTTPException(404, "Vacancy not found")
                if change.status in ("applied", "not_now"):
                    kind = "apply" if change.status == "applied" else "not_now"
                    applications.enqueue(vacancy_id, kind=kind, fresh=previous.user_status == "new")
                else:
                    applications.withdraw(vacancy_id, hide=change.status == "new")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ApplyError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"ok": True, "feed_count": storage.vacancies.feed_count()}

    # ---- the user's Drive folder for vacancy folders (Google Picker, once)
    parent = DriveParent(config.paths.data / PARENT_FILE)

    def google_drive() -> GoogleDrive:
        drive = create_drive(config, f"http://127.0.0.1:{port}/drive/pick")
        if not isinstance(drive, GoogleDrive):
            raise HTTPException(409, drive.reason or "Google Drive is not connected")
        return drive

    @app.get("/drive/pick", response_class=HTMLResponse)
    def drive_pick(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "drive_pick.html", {"current": parent.get()})

    @app.post("/api/drive/picker")
    def drive_picker() -> dict[str, Any]:
        """For the picker page only (POST: the origin check applies). The OAuth client id starts with the project number."""
        secrets = Secrets.load(config.paths.config / ".env")  # read now: a key typed in after the start counts
        key = secrets.get(config.apply.drive.picker_key_env)
        if not key:
            raise HTTPException(409, f"Set {config.apply.drive.picker_key_env} in config/.env (docs/APPLY.md)")
        client_id = secrets.get("GOOGLE_OAUTH_CLIENT_ID") or ""
        try:
            token = google_drive().access_token()
        except ApplyError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"token": token, "key": key, "app_id": client_id.split("-", 1)[0]}

    @app.post("/api/drive/parent")
    def drive_parent(choice: DriveChoice) -> dict[str, Any]:
        name = google_drive().folder_name(choice.id)
        if name is None:
            raise HTTPException(400, "The app cannot open this folder — pick it again")
        parent.save(choice.id, name)
        return {"ok": True, "name": name}

    @app.get("/api/applications")
    def list_applications() -> dict[str, Any]:
        return {"enabled": applications.enabled,
                "sections": views.sidebar_sections(config, storage, applications.visible())}

    def _application(application_id: int) -> Any:
        found = applications.get(application_id)
        if found is None:
            raise HTTPException(404, "Application not found")
        return found

    @app.post("/api/applications/{application_id}/retry")
    def retry_application(application_id: int) -> dict[str, Any]:
        _application(application_id)
        if not applications.retry(application_id):
            raise HTTPException(409, "The application cannot be retried in its current state")
        return {"ok": True}

    @app.post("/api/applications/{application_id}/stop")
    def stop_application(application_id: int) -> dict[str, Any]:
        return {"ok": applications.stop(_application(application_id).id)}

    @app.post("/api/applications/{application_id}/generate")
    def generate_application(application_id: int, choice: GenerateChoice) -> dict[str, Any]:
        found = _application(application_id)
        manual = views.is_manual(config, storage.vacancies.get(found.vacancy_id))
        if not applications.generate(application_id, choice.cv, choice.letter, manual):
            raise HTTPException(409, "Only a vacancy waiting in the purgatory can be generated")
        return {"ok": True}

    @app.post("/api/applications/manual")
    def add_manual_vacancy(entry: ManualVacancyInput) -> dict[str, Any]:
        """Add the manually entered vacancy and queue its requested or provided materials."""
        if not applications.enabled:
            raise HTTPException(409, "Apply is off (config/apply.yaml → enabled)")
        try:
            pdf = manual_cv.resolve(entry.pdf_token) if entry.pdf_token else None
            added = ManualIntake(storage).create(entry.title, entry.url, entry.description, entry.company,
                                                 cv=entry.cv, letter=entry.letter, pdf=pdf,
                                                 cover_letter=entry.cover_letter)
        except ManualError as exc:
            raise HTTPException(400, str(exc)) from exc
        applications.wake_worker()
        return {"ok": True, "application_id": added.application.id, "vacancy_id": added.vacancy_id}

    @app.post("/api/applications/manual/cv")
    def pick_manual_cv() -> dict[str, Any]:
        if not applications.enabled:
            raise HTTPException(409, "Apply is off (config/apply.yaml → enabled)")
        try:
            return {"file": manual_cv.pick()}
        except ManualError as exc:
            raise HTTPException(400, str(exc)) from exc

    def reveal_cv(path: Path) -> dict[str, bool]:
        try:
            if not reveal_in_finder(path):
                raise HTTPException(404, "The PDF is not there")
        except ApplyError as exc:
            raise HTTPException(500, str(exc)) from exc
        return {"ok": True}

    @app.post("/api/applications/manual/cv/{token}/reveal")
    def reveal_manual_cv(token: str) -> dict[str, bool]:
        try:
            path = manual_cv.resolve(token)
        except ManualError as exc:
            raise HTTPException(404, str(exc)) from exc
        return reveal_cv(path)

    @app.delete("/api/applications/manual/cv/{token}")
    def clear_manual_cv(token: str) -> dict[str, bool]:
        manual_cv.clear(token)
        return {"ok": True}

    @app.post("/api/applications/{application_id}/reveal")
    def reveal_pdf(application_id: int) -> dict[str, bool]:
        found = _application(application_id)
        if not found.pdf_path:
            raise HTTPException(404, "The PDF is not there")
        return reveal_cv(Path(found.pdf_path))

    @app.get("/api/applications/{application_id}/letter")
    def application_letter(application_id: int) -> dict[str, str]:
        found = _application(application_id)
        if not found.letter:
            raise HTTPException(404, "The letter is not ready yet")
        return {"text": found.letter}

    @app.post("/api/vacancies/{vacancy_id}/report")
    def report_vacancy(vacancy_id: int, report: Report) -> dict[str, Any]:
        if not report.text.strip():
            raise HTTPException(400, "Write what is wrong with this evaluation")
        if not storage.vacancies.save_report(vacancy_id, report.text):
            raise HTTPException(404, "Vacancy not found")
        return {"ok": True, "reported": storage.vacancies.reports_count()}

    @app.post("/api/notices/{notice_id}/dismiss")
    def dismiss_notice(notice_id: str) -> dict[str, Any]:
        if not notices.dismiss(config.paths.notices, notice_id):
            raise HTTPException(404, "Notice not found")
        return {"ok": True}

    settings = SettingsService(config)

    @app.get("/api/settings")
    def read_settings() -> dict[str, Any]:
        return settings.snapshot()

    @app.post("/api/settings/linkedin-pages/off")
    def linkedin_pages_off() -> dict[str, Any]:
        settings.linkedin_pages_off()
        return settings.linkedin_pages()

    @app.post("/api/settings/linkedin-pages/on")
    def linkedin_pages_on(body: LinkedInPagesOn) -> dict[str, Any]:
        try:
            settings.linkedin_pages_on(body.confirm)
        except LinkedInSwitchError as exc:
            raise HTTPException(422, str(exc)) from exc
        return settings.linkedin_pages()

    @app.post("/api/settings/preview")
    def preview_settings(body: SettingsChanges) -> Any:
        try:
            return {"fit": settings.preview_fit(body.changes)}
        except SettingsError as exc:
            return JSONResponse({"detail": "Some values are not valid", "problems": exc.problems}, status_code=422)

    @app.post("/api/settings")
    def save_settings(body: SettingsChanges) -> Any:
        try:
            result = settings.save(body.changes)
        except SettingsError as exc:
            return JSONResponse({"detail": "Nothing saved: some values are not valid", "problems": exc.problems},
                                status_code=422)
        recalculated = None
        if result.rules_changed and manager.is_running():
            manager.recalculate_after_run()
        elif result.rules_changed:  # the feed follows the new rules at once, no LLM
            done = recalculate(config, storage)
            recalculated = {"total": done.total, "changed": done.changed}
        return {"files": result.files, "recalculated": recalculated, "settings": settings.snapshot(),
                "run_active": manager.is_running()}

    @app.get("/icons/{source_id}")
    def icon(source_id: str) -> FileResponse:
        if source_id.startswith("mail-"):
            path = icons.resolve(SourceConfig(id=source_id, type="email", name=source_id))
            return FileResponse(path or STATIC_ICONS / "mail.svg", headers={"Cache-Control": "max-age=3600"})
        source = next((s for s in config.sources.sources if s.id == source_id), None)
        path = icons.resolve(source) if source else None
        if path is None:
            kind = source.type if source else ""
            fallback = {"telegram": "telegram.png", "email": "mail.svg"}.get(kind, "globe.svg")
            path = STATIC_ICONS / fallback
        return FileResponse(path, headers={"Cache-Control": "max-age=3600"})

    return app
