from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..base import CollectContext, Collector, CollectResult, RawItem
from ..registry import register

BASE_URL = "https://changellenge.com"
DEFAULT_LISTING = (
    f"{BASE_URL}/vacancy/filter/work_experience-is-less_year-or-no_experience/"
    "type_vacancy-is-vacancy/indystry-is-analytics/apply/"
)
MAX_DETAIL_CHARS = 8000
_WS = re.compile(r"[ \t\r\f\v\xa0]+")


_BLOCKS = ("p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table", "blockquote", "pre")
_BULLET = "•"


def html_to_text(node: Tag) -> str:
    for br in node.find_all("br"):
        br.replace_with("\n")
    for block in node.find_all(_BLOCKS):
        block.insert_before("\n")
        block.insert_after("\n")
    for li in node.find_all("li"):
        li.insert(0, f"{_BULLET} ")
    lines: list[str] = []
    for raw in node.get_text().splitlines():
        line = _WS.sub(" ", raw).strip()
        if not line:
            continue
        if lines and lines[-1] == _BULLET:
            lines[-1] = f"{_BULLET} {line}"
        else:
            lines.append(line)
    return "\n".join(lines)[:MAX_DETAIL_CHARS]


def parse_listing(html: str) -> tuple[list[RawItem], str | None]:
    """Vacancy cards of one listing page and the pagination variable for "load more" (e.g. PAGEN_1)."""
    soup = BeautifulSoup(html, "lxml")
    items = [_card_item(card) for card in soup.select("li.new-vacancies-card[data-id]")]
    more = soup.select_one("[btn-load-more][data-nav-num]")
    return [item for item in items if item], (more["data-nav-num"] if more else None)


def _card_item(card: Tag) -> RawItem | None:
    link = card.select_one("a.new-vacancies-card__content-link[href]")
    title_tag = card.select_one(".new-vacancies-card__title")
    if not link or not title_tag:
        return None
    title = title_tag.get_text(" ", strip=True)
    tags = [li.get_text(" ", strip=True) for li in card.select(".new-vacancies-card__breadcrumb")]
    url = urljoin(BASE_URL, link["href"])
    return RawItem(
        source_id="",
        external_id=card["data-id"],
        url=url,
        title=title,
        text="\n".join([title, *tags]),
        published_at=None,
        detail_url=url,
        meta={"company": None, "location": tags[0] if tags else None},
    )


def parse_detail(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    head: list[str] = []
    title = soup.select_one("h1.job-title")
    if title:
        head.append(title.get_text(" ", strip=True))
    company = soup.select_one('[itemprop="hiringOrganization"] meta[itemprop="name"]')
    if company and company.get("content"):
        head.append(f"Компания: {company['content']}")
    posted = soup.select_one('meta[itemprop="datePosted"]')
    if posted and posted.get("content"):
        head.append(f"Опубликовано: {posted['content']}")
    salary = soup.select_one(".actions-card .salary")
    if salary:
        head.append(salary.get_text(" ", strip=True))
    for row in soup.select(".actions-card ul.meta li"):
        label, value = row.select_one(".meta__label"), row.select_one(".meta__value")
        if label and value:
            head.append(f"{label.get_text(strip=True)}: {value.get_text(' ', strip=True)}")
    body = soup.select_one("article.description")
    text = "\n".join(head)
    if body:
        text = f"{text}\n\n{html_to_text(body)}"
    return text[:MAX_DETAIL_CHARS].strip()


@register
class ChangellengeCollector(Collector):
    """Changellenge >> vacancy listings: no dates on cards, "load more" via Bitrix PAGEN_N."""

    type_name = "changellenge"

    def listings(self) -> list[str]:
        return list(self.source.params.get("listings") or [self.source.url or DEFAULT_LISTING])

    def collect(self, ctx: CollectContext) -> CollectResult:
        found: dict[str, RawItem] = {}
        truncated = False
        pages_done = 0
        for listing in self.listings():
            done, pages = self._collect_listing(ctx, listing, found, pages_done)
            pages_done += pages
            truncated |= not done
            if ctx.limit and len(found) >= ctx.limit:
                break
        items = list(found.values())[: ctx.limit] if ctx.limit else list(found.values())
        return CollectResult(items=items, status="truncated" if truncated else "ok")

    def _collect_listing(
        self, ctx: CollectContext, listing: str, found: dict[str, RawItem], pages_before: int
    ) -> tuple[bool, int]:
        nav: str | None = None
        for page in range(1, ctx.max_pages + 1):
            ctx.check_cancel()
            ctx.on_page(pages_before + page)
            url = listing if page == 1 else f"{listing}{'&' if '?' in listing else '?'}{nav}={page}"
            cards, nav = parse_listing(self.http.get_text(url))
            fresh = [card for card in cards if card.external_id not in found]
            if not fresh or all(ctx.is_known(card.external_id) for card in fresh):
                return True, page
            for card in fresh:
                card.source_id = self.source.id
                found[card.external_id] = card
                if ctx.limit and len(found) >= ctx.limit:
                    return True, page
            if not nav:
                return True, page
        return False, ctx.max_pages

    def fetch_detail(self, item: RawItem) -> str | None:
        return parse_detail(self.http.get_text(item.detail_url or item.url)) or None

    def icon_url(self) -> str | None:
        return f"{BASE_URL}/icons/favicon.ico"

