from __future__ import annotations

import hashlib
import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Literal

from ..config import SourceConfig
from .http import PoliteHttpClient

CollectStatus = Literal["ok", "truncated"]


class RunCancelled(Exception):
    """Raised inside collectors when the user pressed Cancel."""


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

    def icon_url(self) -> str | None:
        """Where to download this source's icon from (favicon / channel avatar)."""
        return None


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
