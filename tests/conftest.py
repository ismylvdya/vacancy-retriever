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


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never use the network (CLAUDE.md). Before 28.10.2026 every pipeline test downloaded CBR rates: 6–10 s
    each and a flaky cancel test. Mocked transports (httpx.MockTransport) are not affected."""
    import httpx

    def refuse(self, request):
        raise httpx.ConnectError(f"tests must not use the network: {request.url.host}", request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    # The pipeline asks for today's rates on every run: in tests there are none (salary tests use saved files).
    monkeypatch.setattr("vacancy_retriever.rates.RatesStore._download", staticmethod(lambda http: None))


@pytest.fixture(autouse=True)
def every_feed_group_on(request, monkeypatch):
    """Tests read the real config/rules.yaml, where the user turns feed groups on and off (Settings → Feed groups).
    Those switches must not change what other tests check; test_feed_groups.py checks the switches themselves."""
    if request.module.__name__.endswith("test_feed_groups"):
        return
    from vacancy_retriever.config import GroupInfo

    monkeypatch.setattr(GroupInfo, "enabled", property(lambda self: True, lambda self, value: None), raising=False)
