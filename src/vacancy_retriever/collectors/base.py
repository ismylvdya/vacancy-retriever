from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, ClassVar, Literal

from ..config import SourceConfig
from .http import PoliteHttpClient

CollectStatus = Literal["ok", "truncated"]


class RunCancelled(Exception):
    """Raised inside collectors when the user pressed Cancel."""


class SourceNotConfigured(Exception):
    """Source needs credentials/settings the user hasn't filled in yet (not an error)."""


class SourceError(Exception):
    """Collector failed. `alert` marks signs of blocking (captcha, 403, login redirect...)."""

    def __init__(self, message: str, alert: str | None = None):
        super().__init__(message)
        self.alert = alert


@dataclass
class RawItem:
    """One collected unit: a Telegram post, an e-mail or a site card."""

    source_id: str
    external_id: str
    url: str
    text: str
    published_at: datetime | None
    title: str = ""
    multi: bool = False
    detail_url: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def text_hash(self) -> str:
        return hashlib.sha1(normalize_text(self.text).encode()).hexdigest()


PAGE_META = "page_meta"


def page_meta_lines(*lines: str | None) -> list[str]:
    """`meta["page_meta"]` of an item: what the page shows above the description — grade tags, «Опыт работы: …»,
    employment. Kept apart from the description: scoring/requirements.py gives the description priority over it."""
    return [line.strip() for line in lines if line and line.strip()]


@dataclass(frozen=True)
class DetailPage:
    """A fetched vacancy page: the description and the data shown above it (`page_meta_lines`)."""

    text: str
    page_meta: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SourceCursor:
    last_id: str | None = None
    last_published_at: datetime | None = None


@dataclass
class CollectContext:
    cursor: SourceCursor
    since: datetime
    max_pages: int
    is_known: Callable[[str], bool]
    cancel_event: threading.Event = field(default_factory=threading.Event)
    limit: int | None = None
    on_page: Callable[[int], None] = lambda page: None
    # E-mail: Message-IDs of unread letters already checked and left alone, and a way to remember new ones.
    mail_checked: Callable[[], set[str]] = lambda: set()
    remember_checked: Callable[[list[str]], None] = lambda ids: None
    # False: every feed group of this source is off in this run — an e-mail source then reads only statuses of
    # applications and internship waves, its vacancy letters stay unread.
    vacancies_wanted: bool = True

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise RunCancelled()


@dataclass
class CollectResult:
    items: list[RawItem]
    status: CollectStatus = "ok"
    new_cursor: SourceCursor | None = None
    icon_url: str | None = None
    note: str | None = None
    seen: int | None = None
    """Items the source actually showed (new + already known). None = len(items).
    0 here while the source usually shows something is a sign of blocking or a changed layout."""


class Collector(ABC):
    """Common interface of every source collector."""

    type_name: ClassVar[str]

    def __init__(self, source: SourceConfig, http: PoliteHttpClient):
        self.source = source
        self.http = http

    @abstractmethod
    def collect(self, ctx: CollectContext) -> CollectResult: ...

    def fetch_detail(self, item: RawItem) -> str | None:
        """Full vacancy description for site cards. Default: nothing to fetch."""
        return None

    def fetch_detail_page(self, item: RawItem) -> DetailPage | None:
        """The description and the page's own data above it. Default: the description alone."""
        text = self.fetch_detail(item)
        return DetailPage(text) if text else None

    def can_fetch_detail(self, url: str, allowed_domains: set[str]) -> bool:
        """The detail page may be opened: a domain from sources.yaml → detail_domains, never a forbidden one."""
        from .http import is_forbidden

        host = (urlparse(url).hostname or "").lower()
        return not is_forbidden(url) and any(host == d or host.endswith("." + d) for d in allowed_domains)

    def detail_alert(self) -> str | None:
        """A problem with detail pages of this source in this run, shown in Statistics as an error; None = none."""
        return None

    def detail_note(self) -> str | None:
        """Information about detail pages of this run (shown in Statistics, not an error)."""
        return None

    def finish_details(self) -> None:
        """After the details stage, whatever happened (e.g. stop the LinkedIn VPN tunnel)."""

    def bind_stop(self, event: threading.Event) -> None:
        """The run's cancel event, for collectors that wait long between requests."""

    def defer_detail(self, item: RawItem) -> date | None:
        """No description now: the day to try another way (the item waits, out of the feed), or None = evaluate now."""
        return None

    def fetch_deferred(self, item: RawItem, waited_days: int) -> tuple[str | None, bool]:
        """A waiting item's turn: (description or None, final). Not final = keep waiting (paused, limit of the day)."""
        return None, True

    def detail_skip_reason(self, item: RawItem, text: str) -> str | None:
        """After the detail page is fetched: a reason to drop the vacancy before the LLM (recruitment closed,
        office in another country…). Default: never."""
        return None

    def icon_url(self) -> str | None:
        """Where to download this source's icon from (favicon / channel avatar)."""
        return None

    def after_commit(self, processed_meta: list[dict[str, Any]]) -> list[str]:
        """Called only after the run is committed, with `meta` of items whose vacancies were all
        evaluated. Returns error messages (e.g. mail flags could not be set)."""
        return []


_WS = re.compile(r"\s+")
_URL = re.compile(r"https?://\S+")


def normalize_text(text: str) -> str:
    text = _URL.sub(" ", text.lower())
    text = re.sub(r"[^\w\s]", " ", text)
    return _WS.sub(" ", text).strip()


def normalize_url(url: str) -> str:
    url = url.strip().split("#")[0]
    url = re.sub(r"[?&](utm_[^=]+|ref|from)=[^&]*", "", url)
    url = re.sub(r"^https?://(www\.)?", "", url)
    url = url.replace("t.me/", "telegram.me/")
    return url.rstrip("/?&").lower()
