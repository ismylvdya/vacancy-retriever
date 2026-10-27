"""Public (logged-out) LinkedIn job pages for vacancies from the user's job-alert e-mails.

Why it is separate from PoliteHttpClient (where linkedin.com stays forbidden): every rule that keeps this anonymous
and harmless lives here, in one place (decision 2026-10-27):

- the URL is built from the numeric job id only: https://www.linkedin.com/jobs/view/<id>/ — links from the letters
  are never followed (they carry midToken / otpToken that sign the user in);
- no cookies are accepted or sent (a cookie jar that refuses everything), no Referer, no redirects are followed —
  a redirect is LinkedIn asking to sign in, and the answer to that is to stop;
- only job pages, never search, profiles or companies; at most `max_per_run` pages a run, 4–8 s apart;
- the first 429 / 999 / redirect / sign-in wall / captcha pauses LinkedIn until the next day (data/linkedin_guest.json)
  — no retries, no proxies, nothing that hides or changes who is asking.
If anything goes wrong the vacancy is simply evaluated from the letter, as before.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from .http import USER_AGENT

log = logging.getLogger(__name__)

JOB_URL = re.compile(r"^https://www\.linkedin\.com/jobs/view/(\d{6,15})/$")
_WALL = re.compile(r"authwall|/uas/login|/checkpoint/|captcha|challenge|Join to view|Sign in to view", re.I)
_SECTIONS = (".show-more-less-html__markup", ".description__text", "section.description")


class GuestBlocked(Exception):
    """LinkedIn refused a logged-out request: stop for today."""


def job_url(job_id: str) -> str:
    if not job_id.isdigit():
        raise ValueError(f"Not a LinkedIn job id: {job_id!r}")
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


@dataclass
class GuestSettings:
    enabled: bool = False
    max_per_run: int = 40
    min_delay: float = 4.0
    max_delay: float = 8.0


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
    return "\n".join([*lines, "", text]).strip()[:8000] if text else None


class LinkedInGuestFetcher:
    def __init__(self, settings: GuestSettings, state_path: Path, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep, today=date.today):
        self.settings = settings
        self._state_path = state_path
        self._sleep = sleep
        self._today = today
        self._fetched = 0
        self._last = 0.0
        jar = http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept": "text/html", "Accept-Language": "en"},
            cookies=jar, follow_redirects=False, timeout=25.0, transport=transport,
        )
        self.alert: str | None = None  # why LinkedIn pages were not read in this run (for Statistics)

    # ---- the pause until tomorrow after any refusal
    def paused_until(self) -> date | None:
        try:
            value = json.loads(self._state_path.read_text(encoding="utf-8")).get("paused_until")
        except (OSError, ValueError):
            return None
        return date.fromisoformat(value) if value else None

    def _pause_until_tomorrow(self, reason: str) -> None:
        until = date.fromordinal(self._today().toordinal() + 1)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps({"paused_until": until.isoformat(), "reason": reason,
                                                "at": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
        self.alert = f"LinkedIn public pages paused until {until:%-d %b}: {reason}"
        log.warning("LinkedIn guest pages paused until %s: %s", until, reason)

    def available(self) -> bool:
        if not self.settings.enabled:
            return False
        paused = self.paused_until()
        if paused and paused > self._today():
            self.alert = self.alert or f"LinkedIn public pages paused until {paused:%-d %b}"
            return False
        if self._fetched >= self.settings.max_per_run:
            self.alert = self.alert or f"LinkedIn public pages: limit of {self.settings.max_per_run} per run reached"
            return False
        return True

    def fetch(self, url: str) -> str | None:
        """Description text, or None (not available, not a job URL, nothing found). Never raises for LinkedIn."""
        if not JOB_URL.match(url) or not self.available():
            return None
        wait = self._last + random.uniform(self.settings.min_delay, self.settings.max_delay) - time.monotonic()
        if self._last and wait > 0:
            self._sleep(wait)
        self._fetched += 1
        try:
            response = self._client.get(url)
        except httpx.HTTPError as exc:
            log.info("LinkedIn guest page %s failed: %s", url, exc)
            return None
        finally:
            self._last = time.monotonic()
        if response.status_code in (429, 999) or response.is_redirect or response.status_code in (401, 403):
            self._pause_until_tomorrow(f"HTTP {response.status_code}"
                                       + (f" → {response.headers.get('location', '')[:60]}" if response.is_redirect else ""))
            return None
        if response.status_code != 200:
            return None  # 404: the posting is gone
        text = parse_job_page(response.text)
        if text is None and _WALL.search(response.text[:20000]):
            self._pause_until_tomorrow("sign-in wall instead of the job page")
        return text
