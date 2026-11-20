"""«Работа России» (trudvsem.ru): the open state API, opendata.trudvsem.ru (decision of the user 20.11.2026).

Every vacancy carries the employer's INN and OGRN — the IT accreditation check takes it straight from the register,
without the LLM (accreditation/stage.py). The API returns full descriptions: no detail pages are fetched.
Contacts of recruiters (names, phones, e-mails) are never stored.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..base import CollectContext, Collector, CollectResult, RawItem, SourceError
from ..registry import register
from ._common import card_text

API_BASE = "https://opendata.trudvsem.ru/api/v1/vacancies"
MOSCOW = "7700000000000"
PAGE_SIZE = 100  # the API maximum
DEFAULT_PARAMS: dict[str, Any] = {"region": MOSCOW, "queries": ["аналитик данных"], "page_size": PAGE_SIZE}


def parse_time(value: str | None) -> datetime | None:
    """`2026-11-03T16:26:00+0300` / `2026-10-08` → UTC."""
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    return None


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text in {".", "-", "—"} else text


def vacancy_text(v: dict[str, Any]) -> str:
    company = v.get("company") or {}
    address = ((v.get("addresses") or {}).get("address") or [{}])[0].get("location")
    requirement = v.get("requirement") or {}
    experience = requirement.get("experience")
    skills = list(dict.fromkeys(v.get("skills") or []))
    return card_text(
        _clean(v.get("job-name")),
        f"Компания: {_clean(company.get('name'))}" if company.get("name") else None,
        f"ИНН работодателя: {company['inn']}" if company.get("inn") else None,
        "Кадровое агентство" if company.get("hr-agency") else None,
        f"Адрес: {address}" if address else None,
        f"Зарплата: {_clean(v.get('salary'))}" if _clean(v.get("salary")) else None,
        f"Занятость: {_clean(v.get('employment'))}; график: {_clean(v.get('schedule'))}"
        if v.get("employment") or v.get("schedule") else None,
        f"Опыт, лет: {experience}" if experience is not None else None,
        f"Образование: {_clean(requirement.get('education'))}" if requirement.get("education") else None,
        f"Навыки: {', '.join(skills)}" if skills else None,
        f"Обязанности: {_clean(v.get('duty'))}" if _clean(v.get("duty")) else None,
        f"Требования: {_clean(v.get('requirements'))}" if _clean(v.get("requirements")) else None,
        f"Условия: {_clean(v.get('conditions'))}" if _clean(v.get("conditions")) else None,
    )


def vacancy_item(v: dict[str, Any], source_id: str = "") -> RawItem:
    company = v.get("company") or {}
    region = (v.get("region") or {}).get("name")
    return RawItem(
        source_id=source_id,
        external_id=str(v["id"]),
        url=str(v.get("vac_url") or f"https://trudvsem.ru/vacancy/card/{company.get('ogrn', '')}/{v['id']}"),
        title=_clean(v.get("job-name")),
        text=vacancy_text(v),
        published_at=parse_time(v.get("date_modify")) or parse_time(v.get("creation-date")),
        meta={"company": _clean(company.get("name")) or None, "location": region,
              "employer_inn": str(company.get("inn") or "") or None,
              "hr_agency": bool(company.get("hr-agency"))},
    )


def parse_vacancies(data: dict[str, Any], source_id: str = "") -> list[RawItem]:
    if str(data.get("status")) != "200":
        raise SourceError(f"trudvsem: API status {data.get('status')}")
    entries = (data.get("results") or {}).get("vacancies") or []
    return [vacancy_item(e["vacancy"], source_id) for e in entries if (e.get("vacancy") or {}).get("id")]


@register
class TrudvsemCollector(Collector):
    """Search by each of `params.queries` in one region, only vacancies modified since the run window."""

    type_name = "trudvsem"

    def params(self) -> dict[str, Any]:
        return {**DEFAULT_PARAMS, **self.source.params}

    def collect(self, ctx: CollectContext) -> CollectResult:
        params = self.params()
        url = f"{(self.source.url or API_BASE).rstrip('/')}/region/{params['region']}"
        since = ctx.since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        items: dict[str, RawItem] = {}
        truncated = False
        pages = 0
        for text in params["queries"]:
            for page in range(ctx.max_pages):
                ctx.check_cancel()
                pages += 1
                ctx.on_page(pages)
                data = self.http.get_json(url, text=text, offset=page, limit=params["page_size"],
                                          modifiedFrom=since)
                cards = parse_vacancies(data, self.source.id)
                for card in cards:
                    if not ctx.is_known(card.external_id):
                        items.setdefault(card.external_id, card)
                total = int((data.get("meta") or {}).get("total") or 0)
                if len(cards) < params["page_size"] or (page + 1) * params["page_size"] >= total:
                    break
                if ctx.limit is not None and len(items) >= ctx.limit:
                    break
            else:
                truncated = True
        result = list(items.values())[: ctx.limit] if ctx.limit else list(items.values())
        return CollectResult(items=result, status="truncated" if truncated else "ok", new_cursor=None)

    def icon_url(self) -> str | None:
        return "https://trudvsem.ru/favicon.ico"
