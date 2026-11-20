"""Career sites of employers (20.11.2026): МТС Банк, Т-Банк, Сбер, ВТБ — parsing the samples and the paging."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from vacancy_retriever.collectors.base import CollectContext, SourceCursor
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.sites.employers import (
    MtsBankCollector,
    SberCollector,
    TbankCollector,
    VtbCollector,
    markdown_text,
    mts_detail,
    tbank_detail,
    vtb_listing,
)
from vacancy_retriever.config import SourceConfig

from .conftest import SAMPLES

H = SAMPLES / "html"
OLD = datetime(2000, 1, 1, tzinfo=UTC)


def ctx(max_pages: int = 3) -> CollectContext:
    return CollectContext(SourceCursor(), OLD, max_pages, lambda _id: False)


class Replay(httpx.BaseTransport):
    def __init__(self, answer):
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)


def http(answer) -> tuple[PoliteHttpClient, Replay]:
    transport = Replay(answer)
    return PoliteHttpClient(min_delay=0, max_delay=0, transport=transport), transport


def test_markdown_text():
    assert markdown_text("\\* one\n\n\n\n**bold**") == "* one\n\nbold"


def test_mtsbank_listing_detail_and_repeated_filters():
    listing = json.loads((H / "mtsbank" / "vacancies_compact.json").read_text(encoding="utf-8"))
    detail = json.loads((H / "mtsbank" / "vacancy_5457.json").read_text(encoding="utf-8"))
    client, transport = http(lambda r: httpx.Response(200, json=listing))
    source = SourceConfig(id="mtsbank", type="mtsbank", name="МТС Банк", params={"city": 138, "specializations": [1, 2]})
    result = MtsBankCollector(source, client).collect(ctx())
    assert len(result.items) == len(listing["data"])
    first = result.items[0]
    assert first.url.startswith("https://job.mtsbank.ru/vacancies/") and first.published_at is not None
    query = transport.requests[0].url.params
    assert query.get_list("specializationId") == ["1", "2"] and query["locationId"] == "138"
    text = mts_detail(detail)
    assert "Обязанности:" in text and "Требования:" in text and "**" not in text


def test_tbank_listing_post_and_detail():
    listing = json.loads((H / "tbank" / "getVacancies.json").read_text(encoding="utf-8"))
    client, transport = http(lambda r: httpx.Response(200, json=listing))
    source = SourceConfig(id="tbank", type="tbank", name="Т-Банк", params={"filter": {"generatedGraphQL": {"x": 1}}})
    result = TbankCollector(source, client).collect(ctx())
    assert len(result.items) == len(listing["payload"]["vacancies"]) and result.status == "ok"
    body = json.loads(transport.requests[0].content)
    assert transport.requests[0].method == "POST" and body["filters"] == {"generatedGraphQL": {"x": 1}}
    assert result.items[0].url.startswith("https://www.tbank.ru/career/it/vacancy/moscow/")
    text = tbank_detail((H / "tbank" / "vacancy_97dc014e.html").read_text(encoding="utf-8"))
    assert "Обязанности" in text and "Требования" in text


def test_sber_listing_with_full_text():
    listing = json.loads((H / "sber" / "publications.json").read_text(encoding="utf-8"))
    listing["data"]["total"] = len(listing["data"]["vacancies"])
    client, transport = http(lambda r: httpx.Response(200, json=listing))
    source = SourceConfig(id="sber", type="sber", name="Сбер", params={"locations": ["m"], "prof_areas": ["a", "b"]})
    result = SberCollector(source, client).collect(ctx())
    assert len(result.items) == len(listing["data"]["vacancies"]) and len(transport.requests) == 1
    first = result.items[0]
    assert first.url.startswith("https://rabota.sber.ru/search/") and "Требования:" in first.text
    assert first.meta["company"] and transport.requests[0].url.params.get_list("profAreas") == ["a", "b"]


def test_vtb_listing_from_page_data():
    html = (H / "vtb" / "career_moscow.html").read_text(encoding="utf-8")
    assert vtb_listing(html)["found"] >= len(vtb_listing(html)["items"])
    client, _ = http(lambda r: httpx.Response(200, text=html, headers={"content-type": "text/html"}))
    source = SourceConfig(id="vtb", type="vtb", name="ВТБ", url="https://rabota-vtb.ru/career?area=1")
    result = VtbCollector(source, client).collect(ctx())
    assert len(result.items) == len(vtb_listing(html)["items"]) > 0
    assert result.items[0].meta["company"] == "ВТБ" and result.items[0].title
