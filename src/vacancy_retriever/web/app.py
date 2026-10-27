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
from ..config import AppConfig, SourceConfig
from .. import notices
from ..icons import IconStore
from ..pipeline.manager import AlreadyRunning, RunManager
from ..pipeline.recalculate import recalculate
from ..pipeline.runner import RunOptions
from ..storage import Storage
from ..timeutil import format_local, from_iso, local, utcnow
from . import views

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent
STATIC_ICONS = WEB_DIR / "static" / "icons"


class StatusChange(BaseModel):
    status: str
    note: str | None = None  # debug mode: the reason of a Skip


class Report(BaseModel):
    text: str


class RealLimit(BaseModel):
    percent: float | None = None


class RunRequest(BaseModel):
    """Run panel: days = how far back (None = since the last run); groups = telegram / sites / email (None = all)."""

    days: int | None = None
    groups: list[str] | None = None


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


def create_app(config: AppConfig, storage: Storage, port: int, manager: RunManager | None = None) -> FastAPI:
    app = FastAPI(title=APP_NAME, version=APP_VERSION, docs_url=None, redoc_url=None, openapi_url=None)
    manager = manager or RunManager(config, storage)
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
        counts = storage.vacancies.processed_counts()
        return templates.TemplateResponse(request, template, {
            "tab": tab, "feed_count": counts.get("new", 0), "status": full_status(),
            "health_problems": health_problems(), "notices": notices.active(config.paths.notices),
            "max_days": config.rules.max_age_days, **context,
        })

    def health_problems() -> int:
        return sum(1 for row in views.latest_source_states(views.health(config, storage, limit=6)) if row.warn)

    @app.get("/", response_class=HTMLResponse)
    def feed(request: Request) -> HTMLResponse:
        cards = views.CardBuilder(config, storage)  # per request: latest run, reports and rates change over time
        built = [cards.build(record) for record in storage.vacancies.feed()]
        return page(request, "feed.html", "feed", groups=views.group_feed(built, config))

    processed_tabs = [("all", "All"), ("applied", "Applied"), ("not_now", "Not now"),
                      ("skipped", "Skipped"), ("auto_skipped", "Auto-skipped")]

    @app.get("/processed", response_class=HTMLResponse)
    def processed(request: Request, filter: str | None = None) -> HTMLResponse:
        if filter == "hidden":
            hidden = views.hidden_items(config, storage)
            return page(request, "processed.html", "processed", runs=[], hidden=hidden, total=0, current="hidden",
                        counts=storage.vacancies.processed_counts(), filters=processed_tabs, facets=[],
                        active=False, query="", reported_count=storage.vacancies.reports_count())
        chosen: dict[str, set[str]] = {}
        for key, value in request.query_params.multi_items():
            if key != "filter" and value:
                chosen.setdefault(key, set()).add(value)
        records = storage.vacancies.processed(filter, limit=10000)
        kept, filters = views.apply_filters(config, records, chosen)
        cards = views.CardBuilder(config, storage)
        built = [cards.build(record) for record in kept]
        query = urlencode([(k, v) for k, values in sorted(chosen.items()) for v in sorted(values)])
        return page(request, "processed.html", "processed", runs=views.group_by_run(built, storage),
                    total=len(built), current=filter or "all", counts=storage.vacancies.processed_counts(),
                    filters=processed_tabs, facets=filters, active=bool(chosen), query=query,
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
        view = view if view in ("sources", "runs") else "sources"
        return page(request, "statistics.html", "statistics", view=view, runs=runs,
                    latest=views.latest_source_states(runs), data=views.tokens(config, storage))

    @app.get("/health")
    def health() -> RedirectResponse:
        return RedirectResponse("/statistics", status_code=301)

    @app.get("/tokens")
    def tokens() -> RedirectResponse:
        return RedirectResponse("/statistics?view=runs", status_code=301)

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        state = full_status()
        state["feed_count"] = storage.vacancies.processed_counts().get("new", 0)
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
        try:
            manager.start(RunOptions(source_ids=source_ids, since=since, trigger="ui"))
        except AlreadyRunning as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"started": True}

    @app.post("/api/runs/{run_id}/real-limit")
    def set_real_limit(run_id: int, value: RealLimit) -> dict[str, Any]:
        if value.percent is not None and not 0 <= value.percent <= 100:
            raise HTTPException(400, "Percent must be 0–100")
        if not storage.runs.set_real_limit(run_id, value.percent):
            raise HTTPException(404, "Run not found")
        return {"ok": True}

    @app.post("/api/cancel")
    def cancel_run() -> dict[str, Any]:
        return {"cancelled": manager.cancel()}

    @app.post("/api/vacancies/{vacancy_id}/status")
    def change_status(vacancy_id: int, change: StatusChange) -> dict[str, Any]:
        try:
            changed = storage.vacancies.set_user_status(vacancy_id, change.status, change.note)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not changed:
            raise HTTPException(404, "Vacancy not found")
        return {"ok": True, "feed_count": storage.vacancies.processed_counts().get("new", 0)}

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
