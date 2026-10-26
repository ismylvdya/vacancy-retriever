from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import SourceConfig

PHASES = {
    "collecting": (0.0, 0.55),
    "prefilter": (0.55, 0.57),
    "details": (0.57, 0.62),
    "scoring": (0.62, 0.95),
    "dedup": (0.95, 0.99),
    "committing": (0.99, 1.0),
}
EMA_ALPHA = 0.3


class SourceStats:
    """Moving average of how long each source takes; persisted in data/source_stats.json."""

    def __init__(self, path: Path):
        self._path = path
        try:
            self._data: dict[str, float] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}

    def weight(self, source: SourceConfig) -> float:
        if source.id in self._data:
            return max(self._data[source.id], 0.5)
        if source.type == "telegram":
            return 2.0 + math.ceil(source.posts_per_day / 20) * 2.5
        if source.type == "email":
            return 5.0
        return 3.0 + source.max_pages * 2.5

    def observe(self, source_id: str, seconds: float) -> None:
        previous = self._data.get(source_id)
        self._data[source_id] = seconds if previous is None else previous * (1 - EMA_ALPHA) + seconds * EMA_ALPHA

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=1, sort_keys=True), encoding="utf-8")


@dataclass
class ProgressState:
    run_id: int | None = None
    running: bool = False
    phase: str = "idle"
    percent: float = 0.0
    current: str | None = None
    done_sources: list[str] = field(default_factory=list)
    # For the progress strip: every source of the run (id + name), which are done / failed, which is going now.
    sources: list[dict[str, str]] = field(default_factory=list)
    done_ids: list[str] = field(default_factory=list)
    error_ids: list[str] = field(default_factory=list)
    current_id: str | None = None
    message: str = ""
    started_at: str | None = None
    pid: int | None = None


class ProgressTracker:
    """Thread-safe run progress for the UI; mirrored to data/progress.json for other processes."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._state = ProgressState()
        self._weights: dict[str, float] = {}
        self._done_weight = 0.0
        self._last_write = 0.0

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return asdict(self._state)

    def start(self, run_id: int, started_at: str, pid: int, weights: dict[str, float],
              sources: list[tuple[str, str]] | None = None) -> None:
        with self._lock:
            self._state = ProgressState(run_id=run_id, running=True, phase="collecting", started_at=started_at, pid=pid,
                                        sources=[{"id": sid, "name": name} for sid, name in sources or []])
            self._weights = weights
            self._done_weight = 0.0
        self._flush(force=True)

    def source_started(self, name: str, source_id: str | None = None) -> None:
        with self._lock:
            self._state.current = name
            self._state.current_id = source_id
        self._flush(force=True)

    def source_page(self, source_id: str, name: str, page: int, max_pages: int) -> None:
        """Moves the bar inside a long source (e.g. 100 pages of a busy channel)."""
        with self._lock:
            total = sum(self._weights.values()) or 1.0
            inside = self._weights.get(source_id, 1.0) * min(page / max(max_pages, 1), 0.95)
            self._set_percent("collecting", (self._done_weight + inside) / total)
            self._state.current = f"{name} · page {page}"
        self._flush()

    def source_done(self, source_id: str, name: str, failed: bool = False) -> None:
        with self._lock:
            self._done_weight += self._weights.get(source_id, 1.0)
            self._state.done_sources.append(name)
            (self._state.error_ids if failed else self._state.done_ids).append(source_id)
            self._state.current = None
            self._state.current_id = None
            total = sum(self._weights.values()) or 1.0
            self._set_percent("collecting", self._done_weight / total)
        self._flush(force=True)

    def phase(self, phase: str, message: str = "", fraction: float = 0.0) -> None:
        with self._lock:
            self._state.phase = phase
            self._state.message = message
            self._state.current = None
            self._state.current_id = None
            self._set_percent(phase, fraction)
        self._flush(force=fraction in (0.0, 1.0))

    def finish(self, phase: str, message: str = "") -> None:
        with self._lock:
            self._state.running = False
            self._state.phase = phase
            self._state.message = message
            self._state.current = None
            if phase == "done":
                self._state.percent = 100.0
        self._flush(force=True)

    def _set_percent(self, phase: str, fraction: float) -> None:
        start, end = PHASES.get(phase, (0.0, 1.0))
        self._state.percent = round(100 * (start + (end - start) * min(max(fraction, 0.0), 1.0)), 1)

    def _flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < 0.5:
            return
        self._last_write = now
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.snapshot(), ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass


def read_progress_file(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
