"""Public (logged-out) LinkedIn job pages — the fallback for e-mailed vacancies Google for Jobs did not find.

Why it is separate from PoliteHttpClient (where linkedin.com stays forbidden): every rule that keeps this anonymous
and harmless lives here, in one place (decision 2026-10-27):

- the URL is built from the numeric job id only: https://www.linkedin.com/jobs/view/<id>/ — links from the letters
  are never followed (they carry midToken / otpToken that sign the user in);
- no cookies are accepted or sent (a cookie jar that refuses everything), no Referer, no redirects are followed —
  a redirect is LinkedIn asking to sign in, and the answer to that is to stop;
- only job pages, never search, profiles or companies; at most `max_per_day` pages a day, 60–180 s apart;
- the first 429 / 999 / 401 / 403 / redirect / sign-in wall / captcha pauses LinkedIn for `pause_days`
  (data/linkedin_guest.json) — no retries, no switching exits or addresses after a refusal;
- pages go only through the app's private exit (collectors/egress.py: a proxy or the Xray tunnel), checked before
  EVERY page; no exit or a failed check → LinkedIn is not opened at all in this run (never from the home network);
- the employer's apply link is only read from the page («Apply: …» line), never opened.
If anything goes wrong the vacancy is simply evaluated from the letter.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import random
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from .. import traffic
from .egress import Egress, EgressError
from .http import USER_AGENT

log = logging.getLogger(__name__)

JOB_URL = re.compile(r"^https://www\.linkedin\.com/jobs/view/(\d{6,15})/$")
_WALL = re.compile(r"authwall|/uas/login|/checkpoint/|captcha|challenge|Join to view|Sign in to view", re.I)
_SECTIONS = (".show-more-less-html__markup", ".description__text", "section.description")
_EXTERNAL_APPLY = re.compile(r"https://www\.linkedin\.com/jobs/view/externalApply/\d+\?[^\"'<>\s]+")
SOURCE_LINE = "Description source: LinkedIn public page"


class GuestBlocked(Exception):
    """LinkedIn refused a logged-out request: stop for today."""


def job_url(job_id: str) -> str:
    if not job_id.isdigit():
        raise ValueError(f"Not a LinkedIn job id: {job_id!r}")
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


@dataclass
class GuestSettings:
    enabled: bool = False
    max_per_day: int = 12
    min_delay: float = 60.0
    max_delay: float = 180.0
    pause_days: int = 2
    require_exit: bool = True


def employer_apply_url(html: str) -> str | None:
    """The employer's own page behind «Apply» (offsite applications): read from the page, never opened."""
    for match in _EXTERNAL_APPLY.finditer(html):
        target = (parse_qs(urlparse(unescape(match.group(0))).query).get("url") or [""])[0]
        host = urlparse(target).hostname or ""
        if target.startswith("https://") and host and not host.endswith("linkedin.com"):
            return target.split("?utm_")[0]
    return None


def parse_job_page(html: str) -> str | None:
    """Description + the "Seniority level / Employment type / Job function / Industries" criteria."""
    soup = BeautifulSoup(html, "lxml")
    lines: list[str] = []
    title = soup.select_one("h1")
    if title:
        lines.append(title.get_text(" ", strip=True))
    company = soup.select_one(".topcard__org-name-link, .top-card-layout__second-subline a")
    if company:
        lines.append(f"Company: {company.get_text(' ', strip=True)}")
    place = soup.select_one(".topcard__flavor--bullet")
    if place:
        lines.append(f"Location: {place.get_text(' ', strip=True)}")
    for item in soup.select(".description__job-criteria-item"):
        name, value = item.select_one("h3"), item.select_one("span")
        if name and value:
            lines.append(f"{name.get_text(' ', strip=True)}: {value.get_text(' ', strip=True)}")
    body = next((node for node in (soup.select_one(css) for css in _SECTIONS) if node), None)
    if body is None:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("@type") == "JobPosting" and data.get("description"):
                body = BeautifulSoup(data["description"], "lxml")
                break
    if body is None:
        return None
    for br in body.find_all("br"):
        br.replace_with("\n")
    for block in body.find_all(["p", "li", "div", "h2", "h3", "ul"]):
        block.insert_after("\n")
    text = "\n".join(line.strip() for line in body.get_text().splitlines() if line.strip())
    if not text:
        return None
    lines.append(SOURCE_LINE)
    apply_url = employer_apply_url(html)
    if apply_url:
        lines.append(f"Apply: {apply_url}")
    return "\n".join([*lines, "", text]).strip()[:8000]


class LinkedInGuestFetcher:
    def __init__(self, settings: GuestSettings, state_path: Path, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], object] | None = None, today: Callable[[], date] = date.today,
                 egress: Egress | None = None, stop_event: threading.Event | None = None):
        self.settings = settings
        self._state_path = state_path
        self._stop_event = stop_event or threading.Event()
        self._sleep = sleep or self._stop_event.wait  # a cancelled run does not wait out a 3-minute pause
        self._today = today
        self._transport = transport
        self._egress = egress
        self._egress_up = False
        self._egress_failed = False
        self._last = 0.0
        self._client: httpx.Client | None = None
        self.fetched = 0
        self.found = 0
        self.alert: str | None = None  # a problem to show in Statistics as an error (exit down, LinkedIn refused)
        self.note: str | None = None   # information (limit of the day reached)

    def _http(self) -> httpx.Client:
        if self._client is None:
            jar = http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
            proxy = self._egress.proxy_url if self._egress and self._egress_up else None
            self._client = httpx.Client(
                headers={"User-Agent": USER_AGENT, "Accept": "text/html", "Accept-Language": "en"},
                cookies=jar, follow_redirects=False, timeout=25.0, transport=self._transport, proxy=proxy,
            )
        return self._client

    def _exit_ok(self) -> bool:
        """The private exit is up and passes its checks right now — otherwise LinkedIn stays closed."""
        if not self.settings.require_exit:
            return True
        if self._egress_failed:
            return False
        if self._egress is None:
            return self._exit_down("no exit configured (mail.yaml → linkedin_public_pages.exit, config/.env)")
        try:
            if not self._egress_up:
                self._egress.start()
                self._egress_up = True
            else:
                self._egress.verify()
        except EgressError as exc:
            return self._exit_down(str(exc))
        return True

    def _exit_down(self, reason: str) -> bool:
        self._egress_failed = True
        self.alert = f"LinkedIn not opened — exit problem: {reason}"
        log.error("LinkedIn pages are not opened in this run: exit problem: %s", reason)
        self.close()
        return False

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._egress is not None and self._egress_up:
            self._egress.stop()
            self._egress_up = False

    # ---- state: the pause after a refusal and the pages counted per day
    def _state(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, **changes: object) -> None:
        data = {**self._state(), **changes}
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(data), encoding="utf-8")

    def paused_until(self) -> date | None:
        value = self._state().get("paused_until")
        return date.fromisoformat(value) if value else None

    def used_today(self) -> int:
        state = self._state()
        return int(state.get("count", 0)) if state.get("day") == self._today().isoformat() else 0

    def _count_page(self) -> None:
        self._save(day=self._today().isoformat(), count=self.used_today() + 1)

    def _pause(self, reason: str) -> None:
        until = self._today() + timedelta(days=self.settings.pause_days)
        self._save(paused_until=until.isoformat(), reason=reason, at=datetime.now().isoformat(timespec="seconds"))
        self.alert = f"LinkedIn public pages paused until {until:%-d %b}: {reason}"
        log.warning("LinkedIn guest pages paused until %s: %s", until, reason)

    def available(self) -> bool:
        if not self.settings.enabled or self._egress_failed or self._stop_event.is_set():
            return False
        paused = self.paused_until()
        if paused and paused > self._today():
            self.alert = self.alert or f"LinkedIn public pages paused until {paused:%-d %b}"
            return False
        if self.used_today() >= self.settings.max_per_day:
            self.note = f"LinkedIn public pages: {self.settings.max_per_day} a day reached"
            return False
        return True

    def fetch(self, url: str) -> str | None:
        """Description text, or None (not available, not a job URL, nothing found). Never raises for LinkedIn."""
        if not JOB_URL.match(url) or not self.available():
            return None
        wait = self._last + random.uniform(self.settings.min_delay, self.settings.max_delay) - time.monotonic()
        if self._last and wait > 0:
            self._sleep(wait)
            if self._stop_event.is_set():
                return None
        if not self._exit_ok():  # checked right before the page, after the pause
            return None
        self.fetched += 1
        self._count_page()
        try:
            response = self._http().get(url)
            traffic.add(traffic.response_size(response))
        except httpx.HTTPError as exc:
            log.info("LinkedIn guest page %s failed: %s", url, exc)
            return None
        finally:
            self._last = time.monotonic()
        if response.status_code in (429, 999) or response.is_redirect or response.status_code in (401, 403):
            self._pause(f"HTTP {response.status_code}"
                        + (f" → {response.headers.get('location', '')[:60]}" if response.is_redirect else ""))
            return None
        if response.status_code != 200:
            return None  # 404: the posting is gone
        text = parse_job_page(response.text)
        if text is None and _WALL.search(response.text[:20000]):
            self._pause("sign-in wall instead of the job page")
        if text:
            self.found += 1
        return text
