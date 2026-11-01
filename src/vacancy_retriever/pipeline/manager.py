from __future__ import annotations

import json
import logging
import threading
from typing import Any

from ..config import AppConfig
from ..llm.factory import create_provider
from ..storage import Storage
from .notify import notify
from .progress import ProgressTracker, read_progress_file
from .run_lock import RunLock
from .runner import Pipeline, RunOptions, RunSummary

log = logging.getLogger(__name__)


class AlreadyRunning(Exception):
    pass


class RunManager:
    """Starts / cancels runs; one at a time across all processes (OS file lock)."""

    def __init__(self, config: AppConfig, storage: Storage):
        self.config = config
        self.storage = storage
        self._lock = RunLock(config.paths.run_lock)
        self._progress_path = config.paths.data / "progress.json"
        self.progress = ProgressTracker(self._progress_path)
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._guard = threading.Lock()
        self.last_error: str | None = None

    def recover(self) -> list[int]:
        """Runs left 'running' by a crash or sleep are rolled back — unless another process is running one."""
        if self._lock.acquire():
            try:
                return self.storage.runs.recover_interrupted()
            finally:
                self._lock.release()
        return []

    def is_running(self) -> bool:
        return (self._thread is not None and self._thread.is_alive()) or self._lock.is_held_elsewhere()

    def start(self, options: RunOptions) -> None:
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                raise AlreadyRunning("A run is already in progress")
            if not self._lock.acquire():
                raise AlreadyRunning("A run is already in progress (started from another window or the terminal)")
            self.storage.runs.recover_interrupted()
            self._cancel = threading.Event()
            self._thread = threading.Thread(target=self._execute, args=(options,), name="run", daemon=True)
            self._thread.start()

    def run_foreground(self, options: RunOptions) -> RunSummary:
        if not self._lock.acquire():
            raise AlreadyRunning("A run is already in progress (in the app or another terminal)")
        try:
            self.storage.runs.recover_interrupted()
            self._cancel = threading.Event()
            return self._pipeline().run(options)
        finally:
            self._lock.release()

    def cancel(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            self._cancel.set()
            return True
        return False

    def cancel_event(self) -> threading.Event:
        return self._cancel

    def status(self) -> dict[str, Any]:
        if self._thread is not None and self._thread.is_alive():
            state = self.progress.snapshot()
            state["owned"] = True
        elif self._lock.is_held_elsewhere():
            state = read_progress_file(self._progress_path) or {"running": True, "phase": "collecting", "percent": 0}
            state["running"] = True
            state["owned"] = False
        else:
            state = self.progress.snapshot()
            state["running"] = False
            state["owned"] = False
        state["last_run"] = self._last_run()
        state["pending_llm"] = self.storage.items.count_pending()
        state["waiting_pages"] = self.storage.items.count_waiting_pages()
        state["last_error"] = self.last_error
        return state

    def _last_run(self) -> dict[str, Any] | None:
        run = self.storage.runs.last_finished()
        if not run:
            return None
        return {
            "id": run["id"],
            "started_at": run["started_at"],
            "finished_at": run["finished_at"],
            "status": run["status"],
            "summary": json.loads(run["summary_json"] or "{}"),
        }

    def _pipeline(self) -> Pipeline:
        return Pipeline(self.config, self.storage, lambda: create_provider(self.config), self.progress,
                        self._cancel, notifier=notify)

    def _execute(self, options: RunOptions) -> None:
        self.last_error = None
        try:
            self._pipeline().run(options)
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._lock.release()
