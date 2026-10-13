from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from vacancy_retriever.collectors.base import CollectContext, SourceCursor
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.telegram import TelegramCollector, enrich_rvc, parse_channel_page
from vacancy_retriever.collectors.base import RawItem
from vacancy_retriever.config import SourceConfig

from .conftest import SAMPLES

TG = SAMPLES / "html" / "telegram"


def _html(name: str) -> str:
    return (TG / f"{name}.html").read_text(encoding="utf-8")


def test_parses_posts_and_avatar():
    posts, avatar = parse_channel_page(_html("serbiawork"), "serbiawork")
    assert len(posts) >= 10
    assert avatar and avatar.startswith("https://")
    first = posts[0]
    assert first.url.startswith("https://telegram.me/serbiawork/")
    assert first.published_at is not None and first.published_at.tzinfo is not None
    assert first.text
    assert all(p.multi for p in posts)


def test_rvc_posts_are_single_vacancy_with_grade_and_languages():
    posts, _ = parse_channel_page(_html("revacancy"), "revacancy")
    vacancies = [p for p in posts if p.meta.get("grade_emoji") in {"🟩", "🟨", "🟥", "🟪", "⬜"}]
    assert vacancies
    assert all(not p.multi for p in posts)
    assert any(p.meta.get("grade") == "junior" for p in vacancies)
    assert any("ru" in p.meta.get("required_languages", []) for p in vacancies)


def test_rvc_first_line_parsing():
    item = RawItem("revacancy", "1", "u", "🟩 Junior Data Analyst (Growth) | 1 year(s) | Junior | IC\n"
                   "Требуемые языки: 🇬🇧 🇷🇺", None)
    enrich_rvc(item)
    assert item.title == "Junior Data Analyst (Growth)"
    assert item.meta["grade"] == "junior"
    assert item.meta["required_languages"] == ["en", "ru"]


class _PagedTransport(httpx.BaseTransport):
    """Serves the sample page; every `before=` page is empty."""

    def __init__(self, html: str):
        self.html = html
        self.requests: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        body = self.html if "before" not in str(request.url) else "<html><div class='tgme_channel_info'></div></html>"
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})


def _collector(transport: httpx.BaseTransport) -> TelegramCollector:
    http = PoliteHttpClient(min_delay=0, max_delay=0, transport=transport)
    return TelegramCollector(SourceConfig(id="serbiawork", type="telegram", name="x"), http)


def test_cursor_stops_at_last_id_inclusive():
    posts, _ = parse_channel_page(_html("serbiawork"), "serbiawork")
    ids = sorted(int(p.external_id) for p in posts)
    cursor_id = ids[len(ids) // 2]
    collector = _collector(_PagedTransport(_html("serbiawork")))
    ctx = CollectContext(
        cursor=SourceCursor(last_id=str(cursor_id)),
        since=datetime(2000, 1, 1, tzinfo=UTC),
        max_pages=5,
        is_known=lambda _id: False,
    )
    result = collector.collect(ctx)
    assert result.status == "ok"
    assert all(int(i.external_id) > cursor_id for i in result.items)
    assert result.new_cursor.last_id == str(ids[-1])


def test_max_pages_marks_truncated():
    collector = _collector(_PagedTransport(_html("serbiawork")))
    ctx = CollectContext(SourceCursor(), datetime(2000, 1, 1, tzinfo=UTC), 1, lambda _id: False)
    assert collector.collect(ctx).status == "truncated"


def test_since_limits_first_run():
    posts, _ = parse_channel_page(_html("serbiawork"), "serbiawork")
    newest = max(p.published_at for p in posts)
    collector = _collector(_PagedTransport(_html("serbiawork")))
    ctx = CollectContext(SourceCursor(), newest - timedelta(seconds=1), 5, lambda _id: False)
    result = collector.collect(ctx)
    assert len(result.items) == 1
    assert result.status == "ok"
