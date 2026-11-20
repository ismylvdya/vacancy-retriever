"""«Работа России» (trudvsem.ru) open API: parsing the sample and the paging of the collector."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from vacancy_retriever.collectors.base import CollectContext, SourceCursor
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.sites.trudvsem import TrudvsemCollector, parse_time, parse_vacancies
from vacancy_retriever.config import SourceConfig

from .conftest import SAMPLES

LIST = json.loads((SAMPLES / "html" / "trudvsem" / "vacancies_page1.json").read_text(encoding="utf-8"))


def test_parse_sample():
    items = parse_vacancies(LIST, "trudvsem")
    assert len(items) == len(LIST["results"]["vacancies"]) > 0
    first = items[0]
    assert first.title and first.url.startswith("https://trudvsem.ru/vacancy/card/")
    assert first.meta["employer_inn"] and first.meta["company"]
    assert f"ИНН работодателя: {first.meta['employer_inn']}" in first.text
    assert first.published_at is not None and first.published_at.tzinfo is not None
    assert all("+7" not in i.text and "@" not in i.text for i in items), "no recruiter contacts"


def test_parse_time():
    assert parse_time("2026-11-03T16:26:00+0300") == datetime(2026, 11, 3, 13, 26, tzinfo=UTC)
    assert parse_time("2026-10-08") == datetime(2026, 10, 8, tzinfo=UTC)
    assert parse_time("") is None


class _Transport(httpx.BaseTransport):
    """Every query has 2 pages of the sample (ids suffixed by query and page)."""

    def __init__(self) -> None:
        self.requests: list[httpx.URL] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url)
        page, text = int(request.url.params["offset"]), request.url.params["text"]
        entries = [{"vacancy": {**e["vacancy"], "id": f"{e['vacancy']['id']}-{text}-{page}"}}
                   for e in LIST["results"]["vacancies"]] if page < 2 else []
        size = len(LIST["results"]["vacancies"])
        return httpx.Response(200, json={"status": "200", "meta": {"total": 2 * size, "limit": size},
                                         "results": {"vacancies": entries}})


def test_collector_walks_every_query_and_pages():
    transport = _Transport()
    size = len(LIST["results"]["vacancies"])
    source = SourceConfig(id="trudvsem", type="trudvsem", name="Работа России", max_pages=5,
                          params={"queries": ["аналитик данных", "python"], "page_size": size})
    http = PoliteHttpClient(min_delay=0, max_delay=0, transport=transport)
    collector = TrudvsemCollector(source, http)
    ctx = CollectContext(SourceCursor(), datetime(2026, 11, 1, tzinfo=UTC), 5, lambda _id: False)
    result = collector.collect(ctx)
    assert len(result.items) == 4 * size and result.status == "ok"
    assert {r.params["text"] for r in transport.requests} == {"аналитик данных", "python"}
    assert all(r.path.endswith("/region/7700000000000") for r in transport.requests)
    assert all(r.params["modifiedFrom"] == "2026-11-01T00:00:00Z" for r in transport.requests)
