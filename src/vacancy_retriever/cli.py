from __future__ import annotations

import argparse
import re
import signal
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .app_info import APP_NAME, APP_SLUG
from .config import AppConfig, ConfigError
from .logging_setup import configure_logging
from .paths import PATHS

DEFAULT_PORT = 18765


def _duration(value: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([hdw])", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("use e.g. 12h, 1d, 2w")
    amount, unit = int(match.group(1)), match.group(2)
    return {"h": timedelta(hours=amount), "d": timedelta(days=amount), "w": timedelta(weeks=amount)}[unit]


def _storage(config: AppConfig, db: str | None):
    from .storage import Storage

    config.paths.ensure()
    if db:
        path = Path(db).resolve()
        return Storage.open(path, path.with_name(path.stem + "_token_log.jsonl"))
    return Storage.open(config.paths.default_db, config.paths.token_log)


def cmd_run(args: argparse.Namespace) -> int:
    from .pipeline.manager import AlreadyRunning, RunManager
    from .pipeline.runner import RunOptions

    config = AppConfig()
    storage = _storage(config, args.db)
    manager = RunManager(config, storage)
    options = RunOptions(
        source_ids=[s.strip() for s in args.sources.split(",")] if args.sources else None,
        limit=args.limit,
        since=args.since,
        trigger="cli",
        notify=not args.no_notify,
    )
    result: dict = {}

    def target() -> None:
        try:
            result["summary"] = manager.run_foreground(options)
        except Exception as exc:  # noqa: BLE001 - printed below
            result["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    signal.signal(signal.SIGINT, lambda *_: (print("\nCancelling…"), manager.cancel_event().set()))
    worker.start()
    last = ""
    while worker.is_alive():
        state = manager.progress.snapshot()
        line = f"[{state['percent']:5.1f}%] {state['phase']}: {state['current'] or state['message']}"
        if line != last:
            print(line, flush=True)
            last = line
        time.sleep(0.5)
    if "error" in result:
        error = result["error"]
        print(f"Error: {error}", file=sys.stderr)
        return 2 if isinstance(error, AlreadyRunning) else 1
    summary = result["summary"]
    print(manager.progress.snapshot()["message"] or summary.status)
    for row in storage.runs.source_runs(summary.run_id):
        if row["status"] not in ("ok",):
            print(f"  {row['source_id']}: {row['status']} {row['error'] or row['note'] or ''}")
    if summary.llm_error:
        print(f"LLM: {summary.llm_error}")
    return 0 if summary.status == "committed" else 1


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web.app import create_app

    config = AppConfig()
    config.validate_all()
    app = create_app(config, _storage(config, args.db), port=args.port)
    app.state.applications.start_worker()
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        app.state.applications.stop_worker()
    return 0


def cmd_linkedin_exit(args: argparse.Namespace) -> int:
    """Checks the private exit of the LinkedIn guest client. Never sends anything to LinkedIn."""
    from .collectors.egress import EgressError
    from .collectors.linkedin_guest import create_egress

    egress = create_egress(AppConfig())
    if egress is None:
        print("No exit configured: mail.yaml → linkedin_public_pages.exit and the proxy URL in config/.env")
        return 2
    try:
        egress.start()
        first = egress.verify()
        egress.verify()
    except EgressError as exc:
        print(f"NOT SAFE — LinkedIn would not be opened: {exc}")
        return 1
    finally:
        egress.stop()
    print(f"OK: {egress.label}; exit {first.v4}{f' / {first.v6}' if first.v6 else ''} is not the home network "
          "and stays the same between checks (sticky session)")
    return 0


def cmd_drive_login(args: argparse.Namespace) -> int:
    from .apply.drive import GoogleToken
    from .apply.google_auth import login
    from .apply.service import TOKEN_FILE

    config = AppConfig()
    client_id, secret = config.secrets.get("GOOGLE_OAUTH_CLIENT_ID"), config.secrets.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not secret:
        print("Put GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET into config/.env (see docs/APPLY.md)")
        return 2
    if args.metadata:
        print("A browser tab opens: also allow READ-ONLY names and dates of your Drive files (for the import of old"
              " applications). Run plain `drive-login` afterwards to go back to the minimal access.")
    else:
        print("A browser tab opens: allow Vacancy Retriever to manage the files it creates in Google Drive.")
    login(GoogleToken(config.paths.data / TOKEN_FILE, client_id, secret), metadata=args.metadata)
    print("Google Drive connected. Restart the app (scripts/stop.sh && scripts/start.sh).")
    return 0


def cmd_menubar(args: argparse.Namespace) -> int:
    from .menubar.app import run_menubar

    run_menubar(args.port)
    return 0


def cmd_open_feed(args: argparse.Namespace) -> int:
    from .menubar.browser import open_feed

    open_feed(f"http://127.0.0.1:{args.port}/", AppConfig().ui.browser)
    return 0


def cmd_check_sources(args: argparse.Namespace) -> int:
    from .collectors.base import CollectContext, SourceCursor, SourceNotConfigured
    from .collectors.http import BlockedError, PoliteHttpClient
    from .collectors.registry import collector_for

    config = AppConfig()
    wanted = set(args.sources.split(",")) if args.sources else None
    http = PoliteHttpClient(min_delay=0.5, max_delay=1.0)
    failures = 0
    for source in config.sources.sources:
        if wanted and source.id not in wanted:
            continue
        if not source.enabled:
            print(f"–  {source.id:28} disabled  {source.disabled_reason or ''}")
            continue
        ctx = CollectContext(SourceCursor(), datetime.now(UTC) - timedelta(days=30), 1, lambda _id: False, limit=3)
        started = time.monotonic()
        try:
            result = collector_for(source, http).collect(ctx)
            print(f"✓  {source.id:28} {len(result.items):3} items  {time.monotonic() - started:4.1f}s")
        except SourceNotConfigured as exc:
            print(f"·  {source.id:28} not configured: {exc}")
        except BlockedError as exc:
            failures += 1
            print(f"✗  {source.id:28} BLOCKED ({exc.alert}): {exc}")
        except Exception as exc:  # noqa: BLE001 - report every source
            failures += 1
            print(f"✗  {source.id:28} {type(exc).__name__}: {exc}")
    http.close()
    return 1 if failures else 0


def cmd_export_email_samples(args: argparse.Namespace) -> int:
    from .collectors.email.export import export_samples

    config = AppConfig()
    return export_samples(config, args.mailbox, args.limit, PATHS.root / "samples" / "emails", raw=args.raw)


def cmd_recalculate(args: argparse.Namespace) -> int:
    from .pipeline.recalculate import recalculate

    config = AppConfig()
    result = recalculate(config, _storage(config, args.db))
    print(f"Recalculated {result.total} vacancies, {result.changed} changed")
    return 0


def cmd_accreditation(args: argparse.Namespace) -> int:
    """IT accreditation of the feed's Russian employers now (the same check every run does after its commit)."""
    from .accreditation.stage import registry_store, run_accreditation
    from .collectors.http import PoliteHttpClient
    from .llm.factory import create_provider

    config = AppConfig()
    storage = _storage(config, args.db)
    http = PoliteHttpClient()
    try:
        if args.registry_only:
            problem = registry_store(config).refresh(http, force=True)
            registry = registry_store(config).load()
            print(problem or f"IT register snapshot {registry.snapshot_date if registry else '—'}: "
                             f"{len(registry.records) if registry else 0} records")
            return 1 if problem else 0
        result = run_accreditation(config, storage, None if args.no_llm else (lambda: create_provider(config)),
                                   http, threading.Event(), recheck_unknown=args.recheck_unknown)
    finally:
        http.close()
    print(f"Updated {result.checked} vacancies, asked the LLM about {result.employers_asked} employers, "
          f"{result.moved} changed group or decision" + (f"; {result.problem}" if result.problem else ""))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_SLUG, description=f"{APP_NAME} — personal job aggregator")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run collection + evaluation once (same pipeline as the button)")
    run.add_argument("--sources", help="comma-separated source ids (default: all enabled)")
    run.add_argument("--limit", type=int, help="max items per source (debugging)")
    run.add_argument("--since", type=_duration, help="collect only this far back, e.g. 1d, 12h (debugging)")
    run.add_argument("--db", help="SQLite file to use instead of data/vacancy_retriever.sqlite")
    run.add_argument("--no-notify", action="store_true", help="no macOS notification")
    run.set_defaults(func=cmd_run)

    serve = sub.add_parser("serve", help="web interface on 127.0.0.1")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--db")
    serve.set_defaults(func=cmd_serve)

    menubar = sub.add_parser("menubar", help="menu bar icon (macOS)")
    menubar.add_argument("--port", type=int, default=DEFAULT_PORT)
    menubar.set_defaults(func=cmd_menubar)

    open_cmd = sub.add_parser("open-feed", help="open the feed in the browser from config/app.yaml")
    open_cmd.add_argument("--port", type=int, default=DEFAULT_PORT)
    open_cmd.set_defaults(func=cmd_open_feed)

    check = sub.add_parser("check-sources", help="quick availability check of every source")
    check.add_argument("--sources")
    check.set_defaults(func=cmd_check_sources)

    export = sub.add_parser("export-email-samples", help="save sample e-mails (read-only) to samples/emails/")
    export.add_argument("--mailbox", help="mailbox id from config/mail.yaml (default: all)")
    export.add_argument("--limit", type=int, default=3, help="samples per sender (0 = all unread)")
    export.add_argument("--raw", action="store_true", help="do not mask personal data (never commit those)")
    export.set_defaults(func=cmd_export_email_samples)

    recalc = sub.add_parser("recalculate", help="re-apply config/rules.yaml to stored facts (no LLM)")
    recalc.add_argument("--db")
    recalc.set_defaults(func=cmd_recalculate)

    accreditation = sub.add_parser("accreditation", help="check IT accreditation of the feed's Russian employers")
    accreditation.add_argument("--db")
    accreditation.add_argument("--no-llm", action="store_true", help="only the cache and the register, no LLM calls")
    accreditation.add_argument("--registry-only", action="store_true", help="only download the newest register")
    accreditation.add_argument("--recheck-unknown", action="store_true",
                               help="ask the LLM again about every «unknown» employer now (not after 14 days)")
    accreditation.set_defaults(func=cmd_accreditation)

    exit_check = sub.add_parser("linkedin-exit", help="check the private exit for LinkedIn pages (no LinkedIn request)")
    exit_check.set_defaults(func=cmd_linkedin_exit)

    drive = sub.add_parser("drive-login", help="connect Google Drive for the Apply sidebar (one-time, opens a browser)")
    drive.add_argument("--metadata", action="store_true",
                       help="also read-only names / dates of all Drive files (one-off import of old applications)")
    drive.set_defaults(func=cmd_drive_login)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.command, args.verbose)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
