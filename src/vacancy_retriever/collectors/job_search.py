"""Descriptions of LinkedIn vacancies from the e-mails WITHOUT any request to LinkedIn: Google for Jobs via paid job
APIs with free tiers — JSearch (OpenWeb Ninja) and SerpApi (decision 2026-10-27).

Google for Jobs collects postings from LinkedIn, Indeed, Glassdoor and employers' own sites; the API returns them with
the full description. We search "title company city" and accept a result only if it is the same vacancy:
- a result link with the same LinkedIn job number → certainly the same;
- otherwise the company must match and the title must be (almost) the same.
No match → None, the vacancy is evaluated from the letter ("E-mail only"), as before.

Only the API hosts are requested (never linkedin.com, never the result links). Every request is counted per calendar
month in data/job_search.json; a provider stops at its `max_per_month` (free tier) and for the rest of the run after a
refusal (bad key 401/403, quota 429). API keys come from config/.env and are never logged (httpx logs are off and our
messages carry only the status code).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .. import traffic

log = logging.getLogger(__name__)

JSEARCH_URL = "https://api.openwebninja.com/jsearch/search-v2"
JSEARCH_USAGE_URL = "https://api.openwebninja.com/usage"  # plan + remaining quota; does not use the quota
SERPAPI_URL = "https://serpapi.com/search.json"
_LINKEDIN_ID = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(\d{6,15})")
_COMPANY_SUFFIX = re.compile(
    r"\b(d\.?o\.?o\.?|doo|llc|ltd|limited|inc|corp|corporation|gmbh|ag|s\.?a\.?|b\.?v\.?|plc|group|company|co)\b\.?", re.I)
_NOISE = re.compile(r"\s*(\([^)]*\)|\[[^\]]*\]|\s[-–—|/]\s.*$)")
_SERBIA = re.compile(r"serbia|srbija|belgrade|beograd|novi sad|niš|kragujevac|subotica", re.I)


@dataclass
class ProviderSettings:
    id: str                 # "jsearch" | "serpapi"
    api_key: str | None
    max_per_month: int


@dataclass
class Found:
    title: str
    company: str
    location: str
    description: str
    links: list[str] = field(default_factory=list)
    via: str = ""


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())


def _company(text: str) -> str:
    return _norm(_COMPANY_SUFFIX.sub(" ", text))


def same_company(a: str, b: str) -> bool:
    x, y = _company(a), _company(b)
    return bool(x and y) and (x == y or (min(len(x), len(y)) >= 4 and (x in y or y in x)))


def same_title(a: str, b: str) -> bool:
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    if x == y or (min(len(x), len(y)) >= 8 and (x in y or y in x)):
        return True
    tx, ty = set(x.split()), set(y.split())
    return len(tx & ty) / len(tx | ty) >= 0.75


def linkedin_ids(links: list[str]) -> set[str]:
    return {m.group(1) for link in links for m in [_LINKEDIN_ID.search(link or "")] if m}


def matches(found: Found, title: str, company: str, linkedin_id: str | None) -> bool:
    ids = linkedin_ids(found.links)
    if linkedin_id and ids:
        return linkedin_id in ids  # a LinkedIn link with another number is another vacancy
    return same_company(found.company, company) and same_title(found.title, title)


def _jsearch_jobs(body: dict) -> list[Found]:
    data = body.get("data") or []
    if isinstance(data, dict):  # search-v2: {"data": {"jobs": [...], "cursor": ...}}
        data = data.get("jobs") or []
    jobs = []
    for job in data:
        place = ", ".join(filter(None, [job.get("job_city"), job.get("job_state"), job.get("job_country")]))
        links = [job.get("job_apply_link") or ""] + [o.get("apply_link") or "" for o in job.get("apply_options") or []]
        jobs.append(Found(job.get("job_title") or "", job.get("employer_name") or "", job.get("job_location") or place,
                          job.get("job_description") or "", links, job.get("job_publisher") or ""))
    return jobs


def _serpapi_jobs(body: dict) -> list[Found]:
    jobs = []
    for job in body.get("jobs_results") or []:
        links = [job.get("share_link") or ""] + [o.get("link") or "" for o in job.get("apply_options") or []]
        jobs.append(Found(job.get("title") or "", job.get("company_name") or "", job.get("location") or "",
                          job.get("description") or "", links, job.get("via") or ""))
    return jobs


def simplified_title(title: str) -> str:
    """"Data Scientist (Computer Vision)" → "Data Scientist"; "Analyst – Finance | Remote" → "Analyst"."""
    return " ".join(_NOISE.sub(" ", title).split()) or title


def _clean(text: str) -> str:
    """Some descriptions carry half an emoji (a lone surrogate): SQLite cannot store it → "?"."""
    return text.encode("utf-8", "replace").decode("utf-8")


SOURCE_LINE = "Description source: Google for Jobs"
_JOB_BOARDS = re.compile(r"(^|\.)(linkedin|indeed|glassdoor|google|ziprecruiter|jooble|talent|bebee)\.", re.I)


def employer_links(links: list[str], limit: int = 2) -> list[str]:
    """Apply links that lead to the employer's own page (kept for the vacancy note; never opened by the app)."""
    result = []
    for link in links:
        host = urlparse(link).hostname or ""
        if link.startswith("https://") and host and not _JOB_BOARDS.search(host) and link not in result:
            result.append(link.split("?utm_")[0])
    return result[:limit]


def format_found(found: Found) -> str:
    lines = [found.title, f"Company: {found.company}"]
    if found.location:
        lines.append(f"Location: {found.location}")
    lines.append(f"{SOURCE_LINE}{f' (via {found.via})' if found.via else ''}")
    lines += [f"Apply: {link}" for link in employer_links(found.links)]
    return _clean("\n".join([*lines, "", found.description.strip()]).strip()[:8000])


class JobSearchLookup:
    def __init__(self, providers: list[ProviderSettings], usage_path: Path, max_per_run: int,
                 transport: httpx.BaseTransport | None = None, today: Callable[[], date] = date.today,
                 second_query: bool = True):
        self._providers = [p for p in providers if p.api_key]
        self._missing = [p.id for p in providers if not p.api_key]
        self._usage_path = usage_path
        self._max_per_run = max_per_run
        self._transport = transport
        self._today = today
        self._second_query = second_query
        self._stopped: set[str] = set()
        self._remote_left: dict[str, int | None] = {}  # what the provider itself says is left (JSearch /usage)
        self._used = 0
        self._client: httpx.Client | None = None
        self.found = 0
        self.not_found = 0
        self.alert: str | None = None  # an error for Statistics (bad key, quota refused)
        self._limit: str | None = None  # why lookups stopped (not configured, limits reached)

    @property
    def note(self) -> str | None:
        """Information for Statistics: how many were found, and why lookups stopped."""
        done = (f"LinkedIn descriptions: found {self.found}, not found {self.not_found} (Google for Jobs)"
                if self.found or self.not_found else None)
        return "; ".join(filter(None, [done, self._limit])) or None

    # ---- monthly usage (free tiers)
    def _month(self) -> str:
        return self._today().strftime("%Y-%m")

    def usage(self) -> dict[str, int]:
        try:
            data = json.loads(self._usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data.get(self._month(), {})

    def _count(self, provider_id: str) -> None:
        try:
            data = json.loads(self._usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        month = data.setdefault(self._month(), {})
        month[provider_id] = month.get(provider_id, 0) + 1
        self._usage_path.parent.mkdir(parents=True, exist_ok=True)
        self._usage_path.write_text(json.dumps(data, indent=1), encoding="utf-8")

    def _remote(self, provider: ProviderSettings) -> int | None:
        """Requests left in the provider's own billing period (JSearch: 27th to 27th, not the calendar month).
        Asked once per run; None = unknown, then only our own monthly count limits."""
        if provider.id not in self._remote_left:
            left = None
            if provider.id == "jsearch":
                try:
                    response = self._http().get(JSEARCH_USAGE_URL, params={"api_id": "jsearch"},
                                                headers={"x-api-key": provider.api_key})
                    quotas = (response.json().get("data") or {}).get("quotas") or []
                    left = int(quotas[0]["remaining"]) if response.status_code == 200 and quotas else None
                except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
                    left = None
            self._remote_left[provider.id] = left
        return self._remote_left[provider.id]

    def _ready(self) -> list[ProviderSettings]:
        used = self.usage()
        return [p for p in self._providers if p.id not in self._stopped and used.get(p.id, 0) < p.max_per_month
                and (self._remote(p) is None or self._remote(p) > 0)]

    def available(self) -> bool:
        if not self._providers:
            self._limit = (f"LinkedIn descriptions not configured: no API key ({', '.join(self._missing) or 'no providers'}"
                         " — config/.env)")
            return False
        if self._used >= self._max_per_run:
            self._limit = f"LinkedIn descriptions: limit of {self._max_per_run} lookups per run reached"
            return False
        if not self._ready():
            self._limit = ("LinkedIn descriptions: no job API left for this run" if self._stopped
                           else "LinkedIn descriptions: monthly free limits of the job APIs are used up")
            return False
        return True

    # ---- the lookup
    def lookup(self, title: str, company: str, location: str = "", linkedin_id: str | None = None) -> str | None:
        """Description of exactly this vacancy, or None. Never raises for API problems."""
        if not title or not company or not self.available():
            return None
        city = location.split("(")[0].split(",")[0].strip().removesuffix(" City")
        queries = [" ".join(filter(None, [title, company, city]))]
        short = simplified_title(title)
        if self._second_query and short != title:
            # Google for Jobs answers the same query differently from call to call; a title without the
            # "(Computer Vision)" / "(Hybrid/Remote)" tail is a second, different chance (27.10.2026).
            queries.append(" ".join(filter(None, [short, company, city])))
        serbia = bool(_SERBIA.search(location))
        for query in queries:
            for provider in self._ready():
                if self._used >= self._max_per_run:
                    break
                jobs = self._search(provider, query, serbia)
                if jobs is None:
                    continue  # this provider failed: the next one may still answer
                hit = next((job for job in jobs if job.description and matches(job, title, company, linkedin_id)),
                           None)
                if hit:
                    self.found += 1
                    return format_found(hit)
                log.info("Job search: no match for %r among %s", query,
                         [f"{job.title} | {job.company}" for job in jobs[:3]])
                break  # an honest "not found": do not spend the second provider's quota on the same query
        self.not_found += 1
        return None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=30.0, follow_redirects=False, transport=self._transport)
        return self._client

    def _search(self, provider: ProviderSettings, query: str, serbia: bool) -> list[Found] | None:
        if provider.id == "jsearch":
            params = {"query": query, "num_pages": "1"}  # one page (≤10 results) = one request of the quota
            if serbia:
                params["country"] = "rs"
            request = ("GET", JSEARCH_URL, params, {"x-api-key": provider.api_key})
        else:
            params = {"engine": "google_jobs", "q": query, "hl": "en", "api_key": provider.api_key}
            if serbia:
                params["gl"] = "rs"
            request = ("GET", SERPAPI_URL, params, {})
        method, url, params, headers = request
        self._count(provider.id)  # counted before the answer: a timeout may still have used the quota
        self._used += 1
        if self._remote_left.get(provider.id) is not None:
            self._remote_left[provider.id] -= 1
        try:
            response = self._http().request(method, url, params=params, headers=headers)
            traffic.add(traffic.response_size(response))
        except httpx.HTTPError as exc:
            log.info("Job search %s failed: %s", provider.id, type(exc).__name__)  # no URL: it may carry the key
            return None
        if response.status_code in (401, 403, 429):
            self._stopped.add(provider.id)
            reason = "quota or rate limit" if response.status_code == 429 else "API key refused"
            self.alert = f"LinkedIn descriptions: {provider.id} — HTTP {response.status_code} ({reason})"
            log.warning("Job search %s stopped for this run: HTTP %s", provider.id, response.status_code)
            return None
        if response.status_code != 200:
            log.info("Job search %s: HTTP %s", provider.id, response.status_code)
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        if provider.id == "serpapi" and body.get("error"):
            if "hasn't returned any results" in body["error"]:
                return []
            self._stopped.add(provider.id)
            self.alert = f"LinkedIn descriptions: serpapi — {body['error'][:120]}"
            return None
        return _jsearch_jobs(body) if provider.id == "jsearch" else _serpapi_jobs(body)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
