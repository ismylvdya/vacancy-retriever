from __future__ import annotations

import logging
import random
import ssl
import re
import threading
import time
from urllib.parse import urlparse

import httpx

from .. import traffic

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
FORBIDDEN_DOMAINS = ("linkedin.com", "lnkd.in")

_CAPTCHA = re.compile(r"captcha|recaptcha|hcaptcha|smartcaptcha|are you a robot|подтвердите, что вы не робот", re.I)
_CLOUDFLARE = re.compile(r"cf-chl|challenge-platform|Just a moment\.\.\.|Attention Required! \| Cloudflare", re.I)
_LOGIN_PATH = re.compile(r"/(login|signin|sign_in|auth|account/login)\b", re.I)


class BlockedError(Exception):
    def __init__(self, message: str, alert: str):
        super().__init__(message)
        self.alert = alert


class ForbiddenDomainError(Exception):
    pass


def is_forbidden(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in FORBIDDEN_DOMAINS)


def _system_trust() -> ssl.SSLContext | bool:
    """Certificates are checked by the system store (macOS Keychain), like Safari — decision of the user 20.11.2026:
    job.mtsbank.ru and tbank.ru are signed by the Russian Trusted Root CA, which is in the Keychain, not in certifi.
    Verification itself is never switched off."""
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return True


class PoliteHttpClient:
    """httpx wrapper: browser UA, pauses between requests, retries, block detection."""

    def __init__(
        self,
        min_delay: float = 1.0,
        max_delay: float = 3.0,
        timeout: float = 25.0,
        retries: int = 2,
        transport: httpx.BaseTransport | None = None,
    ):
        self._min_delay = min_delay
        self._max_delay = max_delay
        self._retries = retries
        self._last_request = 0.0
        self._lock = threading.Lock()
        self._client = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "ru,en;q=0.9,sr;q=0.8",
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            },
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            verify=_system_trust(),
        )

    def close(self) -> None:
        self._client.close()

    def get_text(self, url: str, **params: object) -> str:
        return self.get(url, **params).text

    def get_json(self, url: str, **params: object) -> object:
        response = self.get(url, **params)
        try:
            return response.json()
        except ValueError as exc:
            self._raise_if_blocked(response)
            raise BlockedError(f"Expected JSON from {url}, got {response.headers.get('content-type')}", "not_json") from exc

    def get_bytes(self, url: str) -> bytes:
        return self.get(url).content

    def get(self, url: str, **params: object) -> httpx.Response:
        if is_forbidden(url):
            raise ForbiddenDomainError(f"Refusing to open {url}: domain is on the never-open list")
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            self._pause()
            try:
                target = httpx.URL(url).copy_merge_params(params) if params else url
                response = self._client.get(target)
                traffic.add(traffic.response_size(response))
            except httpx.TransportError as exc:
                last_exc = exc
                log.warning("GET %s failed (%s), attempt %s", url, exc, attempt + 1)
                time.sleep(2 ** attempt * 2)
                continue
            if response.status_code in (429, 502, 503, 504) and attempt < self._retries:
                log.warning("GET %s -> %s, retrying", url, response.status_code)
                time.sleep(2 ** attempt * 5)
                continue
            self._raise_if_blocked(response)
            response.raise_for_status()
            return response
        raise last_exc or RuntimeError(f"GET {url} failed")

    def post_json(self, url: str, body: object) -> object:
        """A JSON POST for sites whose listing is a POST (tbank.ru); same pauses, retries and block checks."""
        if is_forbidden(url):
            raise ForbiddenDomainError(f"Refusing to open {url}: domain is on the never-open list")
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            self._pause()
            try:
                response = self._client.post(url, json=body)
                traffic.add(traffic.response_size(response))
            except httpx.TransportError as exc:
                last_exc = exc
                log.warning("POST %s failed (%s), attempt %s", url, exc, attempt + 1)
                time.sleep(2 ** attempt * 2)
                continue
            if response.status_code in (429, 502, 503, 504) and attempt < self._retries:
                time.sleep(2 ** attempt * 5)
                continue
            self._raise_if_blocked(response)
            response.raise_for_status()
            try:
                return response.json()
            except ValueError as exc:
                raise BlockedError(f"Expected JSON from {url}", "not_json") from exc
        raise last_exc or RuntimeError(f"POST {url} failed")

    def _pause(self) -> None:
        with self._lock:
            wait = self._last_request + random.uniform(self._min_delay, self._max_delay) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    @staticmethod
    def _raise_if_blocked(response: httpx.Response) -> None:
        status = response.status_code
        if status == 403:
            raise BlockedError(f"HTTP 403 from {response.url}", "http_403")
        if status == 429:
            raise BlockedError(f"HTTP 429 (rate limited) from {response.url}", "http_429")
        if response.history and _LOGIN_PATH.search(response.url.path):
            raise BlockedError(f"Redirected to login page {response.url}", "login_redirect")
        content_type = response.headers.get("content-type", "")
        if "html" in content_type:
            head = response.text[:6000]
            if _CLOUDFLARE.search(head):
                raise BlockedError(f"Cloudflare challenge at {response.url}", "cloudflare")
            if status >= 400 or len(response.text) < 20000:
                if _CAPTCHA.search(head):
                    raise BlockedError(f"Captcha at {response.url}", "captcha")
