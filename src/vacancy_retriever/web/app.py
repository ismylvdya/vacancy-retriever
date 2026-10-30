from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
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
from ..apply.models import ApplyError
from ..apply.service import PARENT_FILE, ApplicationService, create_application_service, create_drive, reveal_in_finder
from ..config import Secrets, AppConfig, SourceConfig
from .. import notices
from ..icons import IconStore
from ..pipeline.manager import AlreadyRunning, RunManager
from ..pipeline.recalculate import recalculate
from ..pipeline.runner import RunOptions
from ..storage import Storage
from ..storage.llm_calls import LlmCallRecord
from ..timeutil import format_local, from_iso, local, utcnow
from . import views

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent
STATIC_ICONS = WEB_DIR / "static" / "icons"


class StatusChange(BaseModel):
    status: str


class Report(BaseModel):
    text: str


class DriveChoice(BaseModel):
    id: str


class RunRequest(BaseModel):
    """Run panel: days = how far back (None = since the last run); groups = telegram / sites / email (None = all)."""

    days: int | None = None
    groups: list[str] | None = None
    include_missed: bool = False  # also collect the missed periods of the chosen sources (banner in the panel)


SOURCE_GROUPS = {"telegram": lambda s: s.type == "telegram", "email": lambda s: s.type == "email",
                 "sites": lambda s: s.type not in ("telegram", "email")}


def describe_status(state: dict[str, Any]) -> dict[str, Any]:
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
            groups = " / ".join(str(by_group.get(str(g), 0)) for g in range(1, 6))
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
        return describe_status(manager.status())

    def page(request: Request, template: str, tab: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, template, {
            "tab": tab, "feed_count": storage.vacancies.feed_count(), "status": full_status(),
            "health_problems": health_problems(), "notices": notices.active(config.paths.notices),
            "max_days": config.rules.max_age_days, "missed": missed_summary(),
            "limits": views.claude_limits(config), **context,
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

    @app.get("/", response_class=HTMLResponse)
    def feed(request: Request, stars: str | None = None) -> HTMLResponse:
        """Filters like in Processed, plus the minimum fit (★5 / ★4+ … / all; ★4+ by default, user 28.10.2026)."""
        min_stars = views.FEED_MIN_STARS_DEFAULT if stars is None else (int(stars) if stars.isdigit() else 0)
        chosen: dict[str, set[str]] = {}
        for key, value in request.query_params.multi_items():
            if key != "stars" and value:
                chosen.setdefault(key, set()).add(value)
        records = storage.vacancies.feed()
        above = [r for r in records if (r.stars or 0) >= min_stars]
        kept, facets = views.apply_filters(config, above, chosen, skip={"reason", "fit"})
        cards = views.CardBuilder(config, storage)  # per request: latest run, reports and rates change over time
        built = [cards.build(record) for record in kept]
        return page(request, "feed.html", "feed", groups=views.group_feed(built, config), facets=facets,
                    min_stars=min_stars, star_options=views.star_options(records, min_stars),
                    active=bool(chosen) or min_stars != views.FEED_MIN_STARS_DEFAULT,
                    shown=len(built), all_count=len(records))

    processed_tabs = [("all", "All"), ("applied", "Applied"), ("not_now", "Not now"),
                      ("skipped", "Skipped"), ("auto_skipped", "Auto-skipped")]

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
        search = request.query_params.get("q", "").strip()
        where = request.query_params.get("in", "all")
        where = where if where in dict(views.SEARCH_FIELDS) else "all"
        records = views.search_records(storage, storage.vacancies.processed(filter, limit=10000), search, where)
        kept, filters = views.apply_filters(config, records, chosen)
        cards = views.CardBuilder(config, storage)
        built = [cards.build(record) for record in kept]
        pairs = [(k, v) for k, values in sorted(chosen.items()) for v in sorted(values)]
        query = urlencode(pairs + ([("q", search)] + ([("in", where)] if where != "all" else []) if search else []))
        return page(request, "processed.html", "processed", runs=views.group_by_run(built, storage),
                    total=len(built), current=filter or "all", counts=storage.vacancies.processed_counts(),
                    filters=processed_tabs, facets=filters, active=bool(chosen or search), query=query,
                    search=search, where=where, search_fields=views.SEARCH_FIELDS,
                    reported_count=storage.vacancies.reports_count())

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
        state["limits"] = views.claude_limits(config)
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
        since = timedelta(days=request.days) if request.days else None
        backfill = None
        if request.include_missed:
            chosen = set(source_ids) if source_ids is not None else {s.id for s in config.sources.sources if s.enabled}
            backfill = {s: start for s, (start, _) in storage.runs.missed().items() if s in chosen} or None
        try:
            manager.start(RunOptions(source_ids=source_ids, since=since, backfill=backfill, trigger="ui"))
        except AlreadyRunning as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"started": True}

    @app.post("/api/claude-limits/refresh")
    def refresh_claude_limits() -> dict[str, Any]:
        """One tiny Claude call (Haiku): the subscription limits come back with every reply (llm/limits.py)."""
        if config.llm.provider != "claude_sdk":
            raise HTTPException(409, "Limits are shown only for the Claude subscription (llm.yaml → provider)")
        from ..llm.factory import create_provider

        def probe() -> None:
            started = time.monotonic()
            try:
                response = asyncio.run(create_provider(config).complete_json('Reply with exactly {"ok": true}', "{}",
                                                                              "haiku"))
            except Exception as exc:  # noqa: BLE001 - the bars just stay as they were
                log.info("Claude limits refresh failed: %s", exc)
                return
            usage = response.usage  # recorded like any call: real tokens, and a «before» point for measured %
            storage.llm_calls.record(LlmCallRecord(
                None, "limits", "claude_sdk", usage.model or "haiku", 0, usage.input_tokens, usage.output_tokens,
                usage.cache_read_tokens, usage.cache_creation_tokens, True, time.monotonic() - started,
                five_hour=usage.five_hour, seven_day=usage.seven_day, five_hour_resets_at=usage.five_hour_resets_at))

        threading.Thread(target=probe, name="claude-limits", daemon=True).start()
        return {"ok": True}

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
            changed = storage.vacancies.set_user_status(vacancy_id, change.status)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not changed:
            raise HTTPException(404, "Vacancy not found")
        if change.status == "applied":
            applications.enqueue(vacancy_id)
        elif change.status == "not_now":  # the Obsidian note «не сейчас» with the vacancy's skills (30.10.2026)
            applications.enqueue(vacancy_id, kind="not_now")
        else:
            applications.withdraw(vacancy_id)
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
                "items": [views.application_item(storage, a) for a in applications.visible()]}

    def _application(application_id: int) -> Any:
        found = applications.get(application_id)
        if found is None:
            raise HTTPException(404, "Application not found")
        return found

    @app.post("/api/applications/{application_id}/retry")
    def retry_application(application_id: int) -> dict[str, Any]:
        _application(application_id)
        if not applications.retry(application_id):
            raise HTTPException(409, "It is being processed right now")
        return {"ok": True}

    @app.post("/api/applications/{application_id}/stop")
    def stop_application(application_id: int) -> dict[str, Any]:
        return {"ok": applications.stop(_application(application_id).id)}

    @app.post("/api/applications/{application_id}/dismiss")
    def dismiss_application(application_id: int) -> dict[str, Any]:
        applications.dismiss(_application(application_id).id)
        return {"ok": True}

    @app.post("/api/applications/{application_id}/reveal")
    def reveal_pdf(application_id: int) -> dict[str, Any]:
        found = _application(application_id)
        if not found.pdf_path or not reveal_in_finder(Path(found.pdf_path)):
            raise HTTPException(404, "The PDF is not there")
        return {"ok": True}

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
        if not storage.vacancies.add_report(vacancy_id, report.text):
            raise HTTPException(404, "Vacancy not found")
        return {"ok": True, "reported": storage.vacancies.reports_count()}

    @app.get("/api/vacancies/{vacancy_id}/fulltext")
    def vacancy_text(vacancy_id: int) -> dict[str, str]:
        text = views.full_text(storage, vacancy_id)
        if not text:
            raise HTTPException(404, "Vacancy not found")
        return {"text": text}

    @app.post("/api/notices/{notice_id}/dismiss")
    def dismiss_notice(notice_id: str) -> dict[str, Any]:
        if not notices.dismiss(config.paths.notices, notice_id):
            raise HTTPException(404, "Notice not found")
        return {"ok": True}

    @app.post("/api/recalculate")
    def recalc() -> dict[str, Any]:
        if manager.is_running():
            raise HTTPException(409, "Wait until the run finishes")
        result = recalculate(config, storage)
        return {"total": result.total, "changed": result.changed}

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
