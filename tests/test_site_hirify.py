from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from vacancy_retriever.collectors.base import CollectContext, SourceCursor
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.sites.hirify import (
    HirifyCollector,
    api_listing_url,
    bumped_at,
    parse_detail,
    parse_vacancies,
)
from vacancy_retriever.config import SourceConfig

from .conftest import SAMPLES

DIR = SAMPLES / "html" / "hirify"
LISTING: dict[str, Any] = json.loads((DIR / "vacancies.json").read_text(encoding="utf-8"))
DETAIL: dict[str, Any] = json.loads((DIR / "detail.json").read_text(encoding="utf-8"))
OLD = datetime(2000, 1, 1, tzinfo=UTC)
SERBIA = "https://hirify.me/?countries=serbia&grade=junior,trainee"
REMOTE_RU = "https://hirify.me/?work_format=remote&vacancy_language=ru"


class _ApiTransport(httpx.BaseTransport):
    """Pages of the sample feed with ids shifted per listing and page so every page is new."""

    def __init__(self, last_page: int = 99) -> None:
        self.last_page = last_page
        self.requests: list[httpx.URL] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url)
        if request.url.path.rstrip("/") != "/api/vacancies":
            return httpx.Response(200, json=DETAIL)
        page = int(request.url.params["page"])
        shift = page * 10_000_000 + (1_000_000_000 if "vacancy_language" in request.url.params else 0)
        data = [{**v, "id": v["id"] + shift} for v in LISTING["data"]]
        next_url = None if page >= self.last_page else "https://api.hirify.me/api/vacancies?page=2"
        return httpx.Response(200, json={**LISTING, "data": data, "next_page_url": next_url})


def _collector(transport: httpx.BaseTransport, listings: list[str] | None = None) -> HirifyCollector:
    http = PoliteHttpClient(min_delay=0, max_delay=0, transport=transport)
    source = SourceConfig(id="hirify", type="hirify", name="x", params={"listings": listings or [SERBIA]})
    return HirifyCollector(source, http)


def _ctx(max_pages: int = 3, cursor: SourceCursor | None = None, since: datetime = OLD,
         known: set[str] | None = None, limit: int | None = None) -> CollectContext:
    known = known or set()
    return CollectContext(cursor or SourceCursor(), since, max_pages, known.__contains__, limit=limit)


def test_parses_items():
    items, has_more = parse_vacancies(LISTING)
    assert has_more and len(items) == 15
    first = items[0]
    raw = LISTING["data"][0]
    assert first.external_id == str(raw["id"])
    assert first.url == f"https://hirify.me/jobs/{raw['slug']}"
    assert first.detail_url == f"https://api.hirify.me/api/vacancies/{raw['id']}"
    assert first.text.startswith(first.title)
    assert "Регионы: " in first.text
    assert first.published_at.tzinfo is not None
    bumps = [bumped_at(i) for i in items]
    assert bumps == sorted(bumps, reverse=True)
    json.dumps(first.meta)


def test_placeholder_company_is_dropped():
    items, _ = parse_vacancies(LISTING)
    assert all(not (i.meta["company"] or "").startswith("%") for i in items)
    assert any(i.meta["company"] == "Andersen Lab" for i in items)


def test_reopened_vacancy_uses_reopen_date():
    raw = next(v for v in LISTING["data"] if v["reopened_at"])
    item = next(i for i in parse_vacancies(LISTING)[0] if i.external_id == str(raw["id"]))
    assert item.published_at > datetime.fromisoformat(raw["created_at"])


def test_site_url_maps_to_api():
    assert api_listing_url(SERBIA) == "https://api.hirify.me/api/vacancies?countries=serbia&grade=junior,trainee"


def test_max_pages_per_listing_and_dedupe():
    transport = _ApiTransport()
    result = _collector(transport, [SERBIA, REMOTE_RU]).collect(_ctx(max_pages=2))
    assert result.status == "truncated"
    assert len(transport.requests) == 4
    assert transport.requests[1].params["countries"] == "serbia"
    assert transport.requests[1].params["page"] == "2"
    assert len(result.items) == 60
    assert all(i.source_id == "hirify" for i in result.items)


def test_last_page_is_ok():
    result = _collector(_ApiTransport(last_page=1)).collect(_ctx())
    assert result.status == "ok" and len(result.items) == 15


def test_known_page_stops():
    known = {str(v["id"] + 10_000_000) for v in LISTING["data"]}
    transport = _ApiTransport()
    result = _collector(transport).collect(_ctx(known=known))
    assert result.items == [] and result.status == "ok"
    assert len(transport.requests) == 1


def test_cursor_date_stops_on_bump_date():
    items, _ = parse_vacancies(LISTING)
    cursor = SourceCursor(last_published_at=bumped_at(items[5]) + timedelta(days=1))
    result = _collector(_ApiTransport()).collect(_ctx(cursor=cursor))
    assert result.status == "ok"
    assert len(result.items) <= 6
    assert all(bumped_at(i) >= bumped_at(items[5]) for i in result.items)


def test_since_skips_old_publications():
    items, _ = parse_vacancies(LISTING)
    since = datetime(2026, 9, 1, tzinfo=UTC)
    result = _collector(_ApiTransport(last_page=1)).collect(_ctx(since=since))
    assert result.items
    assert all(i.published_at >= since for i in result.items)
    assert len(result.items) < len(items)


def test_limit():
    result = _collector(_ApiTransport()).collect(_ctx(limit=5))
    assert len(result.items) == 5


def test_detail_text():
    text = parse_detail(DETAIL)
    assert text.startswith(DETAIL["title"])
    assert "<" not in text and len(text) > 500


def test_fetch_detail():
    transport = _ApiTransport()
    item = parse_vacancies(LISTING)[0][0]
    assert _collector(transport).fetch_detail(item)
    assert str(transport.requests[0]) == item.detail_url
