from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _detect_root() -> Path:
    env_root = os.environ.get("VACANCY_RETRIEVER_HOME")
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def prompts(self) -> Path:
        return self.config / "prompts"

    @property
    def user_icons(self) -> Path:
        return self.config / "icons"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def icons(self) -> Path:
        return self.data / "icons"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def cache(self) -> Path:
        return self.root / ".cache"

    @property
    def llm_cwd(self) -> Path:
        return self.cache / "llm_cwd"

    @property
    def default_db(self) -> Path:
        return self.data / "vacancy_retriever.sqlite"

    @property
    def source_stats(self) -> Path:
        return self.data / "source_stats.json"

    @property
    def token_log(self) -> Path:
        return self.data / "token_log.jsonl"

    @property
    def run_lock(self) -> Path:
        return self.data / "run.lock"

    def ensure(self) -> None:
        for path in (self.data, self.icons, self.logs, self.llm_cwd):
            path.mkdir(parents=True, exist_ok=True)


PATHS = ProjectPaths(_detect_root())
