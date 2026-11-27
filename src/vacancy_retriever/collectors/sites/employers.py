"""Career sites of employers (decision of the user 20.11.2026): МТС Банк, Т-Банк, Сбер, ВТБ.

Each one reads the site's own public listing — the JSON its pages load — with the filters the user picked on the site
(`params`, see config/sources.yaml). Every source is collected even when it has few vacancies (user: nothing may be
missed). Ozon (career.ozon.ru, ozon.tech) is not here: both sites answer with an antibot challenge, which is not
bypassed.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

from ..base import CollectContext, Collector, CollectResult, PAGE_META, RawItem, SourceError, page_meta_lines
from ..registry import register
from ._common import ListingCrawler, card_text, html_to_text

MARKDOWN_ESCAPES = re.compile(r"\\([*_\-#>`])")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def markdown_text(value: Any) -> str:
    """The sites keep descriptions as Markdown: `\\*` bullets, `**bold**` → plain lines."""
    text = MARKDOWN_ESCAPES.sub(r"\1", str(value or ""))
    text = text.replace("**", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def with_query(url: str, query: list[tuple[str, str]]) -> str:
    """Repeated keys (`specializationId=1&specializationId=2`) kept as they are."""
    return str(httpx.URL(url, params=query))


class PagedCollector(Collector):
    """Newest-first pages through the shared stop rules (run window, known items, max_pages)."""

    def page(self, page: int) -> tuple[list[RawItem], bool]:  # pragma: no cover - interface
        raise NotImplementedError

    def collect(self, ctx: CollectContext) -> CollectResult:
        crawler = ListingCrawler(ctx)
        crawler.crawl(self.page)
        return crawler.result()


# ---------------------------------------------------------------- МТС Банк (job.mtsbank.ru, Strapi JSON)

MTS_SITE = "https://job.mtsbank.ru"


def mts_item(v: dict[str, Any], source_id: str = "") -> RawItem:
    company = (v.get("company") or {}).get("name")
    location = (v.get("location") or {}).get("shortName") or (v.get("location") or {}).get("name")
    specialization = (v.get("specialization") or {}).get("name")
    url = f"{MTS_SITE}/vacancies/{v['slug']}" if v.get("slug") else f"{MTS_SITE}/vacancies"
    return RawItem(
        source_id=source_id, external_id=str(v["id"]), url=url, title=str(v.get("name") or "").strip(),
        text=card_text(str(v.get("name") or ""), f"Компания: {company}" if company else None,
                       f"Город: {location}" if location else None,
                       f"Направление: {specialization}" if specialization else None),
        published_at=parse_time(v.get("externalPublicationDate") or v.get("publishedAt")),
        detail_url=f"{MTS_SITE}/api/career/vacancies/{v['id']}",
        meta={"company": company, "location": location},
    )


def mts_detail(data: dict[str, Any]) -> str:
    a = (data.get("data") or {}).get("attributes")
    if not a:
        raise SourceError("job.mtsbank.ru: vacancy without attributes (API changed?)")
    salary = " – ".join(str(a[k]) for k in ("salaryFrom", "salaryTo") if a.get(k))
    return card_text(str(a.get("name") or ""), f"Зарплата: {salary}" if salary else None,
                     markdown_text(a.get("introduction")), "Обязанности:\n" + markdown_text(a.get("duties")),
                     "Требования:\n" + markdown_text(a.get("requirements")),
                     "Условия:\n" + markdown_text(a.get("conditions")))


@register
class MtsBankCollector(PagedCollector):
    """`params.specializations`, `params.city` — ids from the site's filter link (job.mtsbank.ru/vacancies?…)."""

    type_name = "mtsbank"
    page_size = 50

    def page(self, page: int) -> tuple[list[RawItem], bool]:
        params = self.source.params
        query = [("sort", "publishedAt:desc"), ("pagination[start]", str((page - 1) * self.page_size)),
                 ("pagination[limit]", str(self.page_size)), ("search", "")]
        query += [("populate", p) for p in ("name", "externalPublicationDate", "vacancy", "location", "specialization")]
        query += [("specializationId", str(s)) for s in params.get("specializations") or []]
        if params.get("city"):
            query.append(("locationId", str(params["city"])))
        data = self.http.get_json(with_query(f"{MTS_SITE}/api/career/vacancies/compact", query))
        if not isinstance(data, dict) or "data" not in data:
            raise SourceError("job.mtsbank.ru: unexpected listing payload")
        items = [mts_item(v, self.source.id) for v in data["data"] if v.get("id")]
        total = int((data.get("meta") or {}).get("total") or 0)
        return items, page * self.page_size < total

    def fetch_detail(self, item: RawItem) -> str | None:
        return mts_detail(self.http.get_json(item.detail_url)) if item.detail_url else None

    def icon_url(self) -> str | None:
        return f"{MTS_SITE}/favicon.ico"


# ---------------------------------------------------------------- Т-Банк (tbank.ru/career, pfpjobs papi)

TBANK_SITE = "https://www.tbank.ru"


def tbank_item(v: dict[str, Any], source_id: str = "") -> RawItem:
    city = "moscow"
    url = f"{TBANK_SITE}/career/it/vacancy/{city}/{(v.get('seoSlug') or 'vacancy').strip()}/{v['urlSlug']}/"
    tags = ", ".join(v.get("tags") or [])
    return RawItem(
        source_id=source_id, external_id=str(v["urlSlug"]), url=url, title=str(v.get("title") or "").strip(),
        text=card_text(str(v.get("title") or "").strip(), "Компания: Т-Банк", v.get("subtitle"),
                       f"Теги: {tags}" if tags else None, v.get("shortDescription")),
        published_at=None, detail_url=url,
        meta={"company": "Т-Банк", "location": v.get("subtitle"),
              PAGE_META: page_meta_lines(f"Теги: {tags}" if tags else None)},
    )


def tbank_detail(html: str) -> str:
    """The vacancy page: the block with «Обязанности» / «Требования» (server-rendered)."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    marker = soup.find(string=lambda s: bool(s) and s.strip() == "Обязанности")
    node = marker.parent if marker else None
    for _ in range(8):
        if node is None:
            break
        text = node.get_text("\n", strip=True)
        if "Требования" in text and len(text) > 800:
            return html_to_text(node)
        node = node.parent
    raise SourceError("tbank.ru: vacancy page has no «Обязанности» block (layout changed?)")


@register
class TbankCollector(Collector):
    """`params.filter` — the body the site posts to /pfpjobs/papi/getVacancies (tag slugs from the filter link).
    The listing has no dates: known vacancies are skipped by id, new ones are taken whole (a few dozen)."""

    type_name = "tbank"

    def collect(self, ctx: CollectContext) -> CollectResult:
        found: dict[str, RawItem] = {}
        offset: int | None = 0
        pages = 0
        while offset is not None and pages < ctx.max_pages:
            ctx.check_cancel()
            pages += 1
            ctx.on_page(pages)
            pagination = {"offset": offset} if offset else {}
            data = self.http.post_json(f"{TBANK_SITE}/pfpjobs/papi/getVacancies",
                                       {"filters": self.source.params["filter"], "pagination": pagination})
            if not isinstance(data, dict) or data.get("resultCode") != "OK":
                raise SourceError(f"tbank.ru: getVacancies {data.get('resultCode') if isinstance(data, dict) else ''}")
            payload = data.get("payload") or {}
            for v in payload.get("vacancies") or []:
                if v.get("urlSlug") and not ctx.is_known(str(v["urlSlug"])):
                    found.setdefault(str(v["urlSlug"]), tbank_item(v, self.source.id))
            nxt = payload.get("nextPagination") or {}
            offset = None if nxt.get("isFinished", True) else int(nxt.get("offset") or 0)
        items = list(found.values())[: ctx.limit] if ctx.limit else list(found.values())
        return CollectResult(items=items, status="truncated" if offset is not None else "ok", new_cursor=None)

    def fetch_detail(self, item: RawItem) -> str | None:
        return tbank_detail(self.http.get_text(item.detail_url)) if item.detail_url else None

    def icon_url(self) -> str | None:
        return f"{TBANK_SITE}/favicon.ico"


# ---------------------------------------------------------------- Сбер (rabota.sber.ru, public API gateway)

SBER_SITE = "https://rabota.sber.ru"
SBER_API = f"{SBER_SITE}/public/app-candidate-public-api-gateway/api/v1/publications"


def sber_item(v: dict[str, Any], source_id: str = "") -> RawItem:
    salary = " – ".join(str(v[k]) for k in ("salary_min", "salary_max") if v.get(k))
    return RawItem(
        source_id=source_id, external_id=str(v.get("internalId") or v["requisitionId"]),
        url=f"{SBER_SITE}/search/{v.get('internalId') or v['requisitionId']}", title=str(v.get("title") or "").strip(),
        text=card_text(str(v.get("title") or "").strip(), f"Компания: {v['company']}" if v.get("company") else None,
                       f"Город: {v['city']}" if v.get("city") else None,
                       f"Направление: {v['specialization']}" if v.get("specialization") else None,
                       f"Зарплата: {salary}" if salary else None, markdown_text(v.get("introduction")),
                       "Обязанности:\n" + markdown_text(v.get("duties")),
                       "Требования:\n" + markdown_text(v.get("requirements")),
                       "Условия:\n" + markdown_text(v.get("conditions"))),
        published_at=parse_time(v.get("publicationDate")),
        meta={"company": v.get("company"), "location": v.get("city")},
    )


@register
class SberCollector(PagedCollector):
    """`params.locations` (FIAS ids), `params.prof_areas` (ids of the site's «Специализация» filter). The listing
    already has the full description: no detail pages."""

    type_name = "sber"
    page_size = 50

    def page(self, page: int) -> tuple[list[RawItem], bool]:
        params = self.source.params
        query = [("skip", str((page - 1) * self.page_size)), ("take", str(self.page_size))]
        query += [("locations", str(x)) for x in params.get("locations") or []]
        query += [("profAreas", str(x)) for x in params.get("prof_areas") or []]
        data = self.http.get_json(with_query(SBER_API, query))
        body = data.get("data") if isinstance(data, dict) else None
        if not isinstance(body, dict):
            raise SourceError("rabota.sber.ru: unexpected listing payload")
        items = [sber_item(v, self.source.id) for v in body.get("vacancies") or []
                 if v.get("internalId") or v.get("requisitionId")]
        return items, page * self.page_size < int(body.get("total") or 0)

    def icon_url(self) -> str | None:
        return f"{SBER_SITE}/favicon.ico"


# ---------------------------------------------------------------- ВТБ (rabota-vtb.ru, Next.js page data)

VTB_SITE = "https://rabota-vtb.ru"


def vtb_listing(html: str) -> dict[str, Any]:
    script = BeautifulSoup(html, "lxml").select_one("#__NEXT_DATA__")
    if script is None or not script.string:
        raise SourceError("rabota-vtb.ru: no page data (layout changed?)")
    fallback = json.loads(script.string)["props"]["pageProps"].get("fallback") or {}
    listings = [v for k, v in fallback.items() if "/vacancies" in k and isinstance(v, dict)]
    if not listings:
        raise SourceError("rabota-vtb.ru: page data has no vacancy listing")
    return listings[0]


def vtb_item(v: dict[str, Any], source_id: str = "") -> RawItem:
    snippet = v.get("snippet") or {}
    salary = v.get("salary") or {}
    salary_text = " – ".join(str(salary[k]) for k in ("from", "to") if salary.get(k))
    address = (v.get("address") or {}).get("raw") or (v.get("area") or {}).get("name")
    department = (v.get("department") or {}).get("name") or (v.get("employer") or {}).get("name") or "Банк ВТБ (ПАО)"
    schedule = (v.get("schedule") or {}).get("name")
    experience = (v.get("experience") or {}).get("name")
    return RawItem(
        source_id=source_id, external_id=str(v["id"]), url=str(v.get("alternate_url") or f"{VTB_SITE}/career"),
        title=str(v.get("name") or "").strip(),
        text=card_text(str(v.get("name") or ""), f"Компания: {department}", f"Адрес: {address}" if address else None,
                       f"Зарплата: {salary_text}" if salary_text else None, f"График: {schedule}" if schedule else None,
                       f"Опыт: {experience}" if experience else None,
                       html_to_text(BeautifulSoup(str(snippet.get("responsibility") or ""), "lxml")),
                       html_to_text(BeautifulSoup(str(snippet.get("requirement") or ""), "lxml"))),
        published_at=parse_time(v.get("published_at") or v.get("created_at")),
        meta={"company": "ВТБ", "location": (v.get("area") or {}).get("name"),
              PAGE_META: page_meta_lines(f"Опыт: {experience}" if experience else None)},
    )


@register
class VtbCollector(PagedCollector):
    """`url` — the filter link of rabota-vtb.ru/career (the page carries the listing in its Next.js data; the
    vacancies themselves are hh.ru ones, which are not opened: only the snippet of the listing is used)."""

    type_name = "vtb"

    def page(self, page: int) -> tuple[list[RawItem], bool]:
        url = self.source.url or f"{VTB_SITE}/career"
        if page > 1:
            url = f"{url}{'&' if '?' in url else '?'}page={page - 1}"
        listing = vtb_listing(self.http.get_text(url))
        items = [vtb_item(v, self.source.id) for v in listing.get("items") or [] if v.get("id")]
        return items, int(listing.get("page") or 0) + 1 < int(listing.get("pages") or 1)

    def icon_url(self) -> str | None:
        return f"{VTB_SITE}/favicon.ico"
