from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

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


class HhDescriptions:
    """Read public copies by HH ID using a separate HTTP session."""

    def __init__(self, transport: httpx.BaseTransport | None = None):
        self._http = PoliteHttpClient(timeout=8, retries=0, transport=transport, follow_redirects=False)
        self._stopped = False

    def close(self) -> None:
        self._http.close()

    def fetch(self, url: str, listing_text: str) -> str | None:
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
        return f'{listing_text.strip()}\n\n{description}'.strip() if description else None
