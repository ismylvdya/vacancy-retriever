from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

from .base import DetailPage
from .http import BlockedError, PoliteHttpClient

log = logging.getLogger(__name__)

MIRROR_HOST = 'get-works.ru'
_HH_VACANCY = re.compile(r'https://(?:www\.)?hh\.ru/vacancy/([0-9]+)/?(?:[?#][^\s]*)?', re.I)


def hh_vacancy_id(url: str) -> str | None:
    match = _HH_VACANCY.fullmatch(url)
    return match.group(1) if match else None


def hh_description_url(url: str) -> str:
    vacancy_id = hh_vacancy_id(url)
    return f'https://{MIRROR_HOST}/job-detail.php?vac={vacancy_id}' if vacancy_id else ''


def parse_description(html: str, vacancy_id: str) -> str | None:
    soup = BeautifulSoup(html, 'lxml')
    for heading in soup.select('h2.detail-title'):
        if heading.get_text(' ', strip=True).casefold() == 'описание вакансии':
            break
    else:
        return None

    description = heading.find_next_sibling()
    if not description or description.name != 'div' or 'p-otstup' not in description.get('class', []):
        return None
    source_links = description.find_next_siblings('a', href=True)
    if vacancy_id not in {hh_vacancy_id(link['href']) for link in source_links}:
        return None

    for tag in description.select('script, style, template, iframe'):
        tag.decompose()
    for tag in description.select('p, li, div, br, h2, h3, h4'):
        tag.insert_after('\n')
    lines = (' '.join(line.split()) for line in description.get_text().splitlines())
    return '\n'.join(filter(None, lines)) or None


_SKIPPED_PROPERTIES = {'дата размещения'}
_EMPTY_VALUES = {'не указана', 'не указано', 'не указан'}


def parse_page_meta(html: str) -> list[str]:
    """«Краткое описание вакансии» of the copy: the properties hh shows under the title («Опыт работы: От 1 года до 3
    лет», «Занятость: …»). hh fills «1–3 года» by default, so the description's own years win (scoring/requirements.py)."""
    soup = BeautifulSoup(html, 'lxml')
    lines = []
    for item in soup.select('ul.ove-detail-list li'):
        label, value = item.find('h5'), item.find('span')
        if not label or not value:
            continue
        name = label.get_text(' ', strip=True).rstrip(':')
        text = ' '.join(value.get_text(' ', strip=True).split()).strip(' ,')
        if text and name.casefold() not in _SKIPPED_PROPERTIES and text.casefold() not in _EMPTY_VALUES:
            lines.append(f'{name}: {text}')
    return lines


class HhDescriptions:
    """Read public copies by HH ID using a separate HTTP session."""

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self._http = PoliteHttpClient(timeout=8, retries=0, transport=transport, follow_redirects=False)
        self._stopped = False

    def close(self) -> None:
        self._http.close()

    def fetch(self, url: str, listing_text: str) -> str | None:
        page = self.fetch_page(url, listing_text)
        return page.text if page else None

    def fetch_page(self, url: str, listing_text: str) -> DetailPage | None:
        vacancy_id = hh_vacancy_id(url)
        if self._stopped or not vacancy_id:
            return None
        try:
            response = self._http.get(hh_description_url(url))
        except httpx.HTTPStatusError as exc:
            if exc.response.is_redirect or exc.response.status_code in (404, 410):
                return None
            self._stopped = True
            log.info('HH descriptions unavailable: Get-Works returned HTTP %s', exc.response.status_code)
            return None
        except (BlockedError, httpx.RequestError) as exc:
            self._stopped = True
            log.info('HH descriptions unavailable: %s', exc)
            return None

        description = parse_description(response.text, vacancy_id)
        if not description:
            return None
        return DetailPage(f'{listing_text.strip()}\n\n{description}'.strip(), parse_page_meta(response.text))
