from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ..app_info import APP_NAME, APP_VERSION
from ..config import AppConfig
from ..icons import IconStore
from ..pipeline.manager import AlreadyRunning, RunManager
from ..pipeline.recalculate import recalculate
from ..pipeline.runner import RunOptions
from ..storage import Storage
from ..timeutil import format_local, from_iso
from . import views

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent
STATIC_ICONS = WEB_DIR / "static" / "icons"


class StatusChange(BaseModel):
    status: str


def describe_status(state: dict[str, Any]) -> dict[str, Any]:
    """Adds the header texts: 'Last run: 13 Oct 08:12 (14 min)' and the run summary line."""
    last = state.get("last_run")
    state["last_run_line"] = "No runs yet"
    state["summary_line"] = ""
    if last:
        started, finished = from_iso(last["started_at"]), from_iso(last["finished_at"])
        minutes = round((finished - started).total_seconds() / 60) if started and finished else 0
        state["last_run_line"] = f"Last run: {format_local(started)} ({minutes} min)"
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
    cards = views.CardBuilder(config, storage)
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
            "tab": tab, "feed_count": counts.get("new", 0), "status": full_status(), **context,
        })

    @app.get("/", response_class=HTMLResponse)
    def feed(request: Request) -> HTMLResponse:
        built = [cards.build(record) for record in storage.vacancies.feed()]
        return page(request, "feed.html", "feed", groups=views.group_feed(built, config))

    @app.get("/processed", response_class=HTMLResponse)
    def processed(request: Request, filter: str | None = None) -> HTMLResponse:
        built = [cards.build(record) for record in storage.vacancies.processed(filter)]
        counts = storage.vacancies.processed_counts()
        return page(request, "processed.html", "processed", cards=built, current=filter or "all",
                    counts=counts, filters=[("all", "All"), ("applied", "Applied"), ("skipped", "Skipped"),
                                            ("not_now", "Not now"), ("auto_skipped", "Auto-skipped")])

    @app.get("/health", response_class=HTMLResponse)
    def health(request: Request) -> HTMLResponse:
        runs = views.health(config, storage)
        return page(request, "health.html", "health", runs=runs, latest=views.latest_source_states(runs))

    @app.get("/tokens", response_class=HTMLResponse)
    def tokens(request: Request) -> HTMLResponse:
        return page(request, "tokens.html", "tokens", data=views.tokens(config, storage))

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        state = full_status()
        state["feed_count"] = storage.vacancies.processed_counts().get("new", 0)
        return state

    @app.post("/api/run")
    def start_run() -> dict[str, Any]:
        try:
            manager.start(RunOptions(trigger="ui"))
        except AlreadyRunning as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"started": True}

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
        return {"ok": True, "feed_count": storage.vacancies.processed_counts().get("new", 0)}

    @app.get("/api/vacancies/{vacancy_id}/fulltext")
    def vacancy_text(vacancy_id: int) -> dict[str, str]:
        text = views.full_text(storage, vacancy_id)
        if not text:
            raise HTTPException(404, "Vacancy not found")
        return {"text": text}

    @app.post("/api/recalculate")
    def recalc() -> dict[str, Any]:
        if manager.is_running():
            raise HTTPException(409, "Wait until the run finishes")
        result = recalculate(config, storage)
        return {"total": result.total, "changed": result.changed}

    @app.get("/icons/{source_id}")
    def icon(source_id: str) -> FileResponse:
        source = next((s for s in config.sources.sources if s.id == source_id), None)
        path = icons.resolve(source) if source else None
        if path is None:
            kind = source.type if source else ""
            fallback = {"telegram": "telegram.svg", "email": "mail.svg"}.get(kind, "globe.svg")
            path = STATIC_ICONS / fallback
        return FileResponse(path, headers={"Cache-Control": "max-age=3600"})

    return app
