from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from vacancy_retriever.config import AppConfig
from vacancy_retriever.paths import ProjectPaths

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
NOW = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def config() -> AppConfig:
    return AppConfig(ProjectPaths(ROOT))


@pytest.fixture
def tmp_paths(tmp_path: Path) -> ProjectPaths:
    """Project paths whose data/ lives in a temp dir while config/ is the real one."""
    for name in ("config",):
        (tmp_path / name).symlink_to(ROOT / name)
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    return paths
