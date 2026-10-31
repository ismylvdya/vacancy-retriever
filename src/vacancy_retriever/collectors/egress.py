"""Where LinkedIn guest requests leave the Mac — never from the home network where the user's account is signed in.

Decision 2026-11-01: the account and the anonymous requests must never share an address. An exit is a proxy URL that
only the LinkedIn guest client of this app uses (never a system VPN, never the browser): a residential proxy from
config/.env (`ProxyEgress`) or the user's VLESS server through a private Xray process (`vpn.XrayTunnel`).
Before EVERY LinkedIn request `ExitGuard.check` asks an IP echo service through the exit and refuses when
- the exit does not answer,
- its IPv4 is the home IPv4, or its IPv6 is inside the home IPv6 /64,
- it differs from the exit seen at the start of the run (a rotating proxy must use a sticky session).
The home addresses are learned directly (without the exit) at the start; a home without IPv6 is fine.
"""

from __future__ import annotations

import ipaddress
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

IPV4_CHECK_URL = "https://api.ipify.org"
IPV6_CHECK_URL = "https://api6.ipify.org"
IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class EgressError(Exception):
    """The exit is not safe or not usable: LinkedIn must not be opened."""


def public_ip(text: str) -> IpAddress:
    address = ipaddress.ip_address(text.strip())  # ValueError for anything that is not an IP
    if address.is_private or address.is_loopback or address.is_link_local:
        raise ValueError(f"not a public address: {address}")
    return address


@dataclass(frozen=True)
class HomeAddresses:
    v4: ipaddress.IPv4Address
    v6: ipaddress.IPv6Network | None  # the home /64, or None when the home network has no IPv6

    def contains(self, address: IpAddress) -> bool:
        if isinstance(address, ipaddress.IPv4Address):
            return address == self.v4
        return self.v6 is not None and address in self.v6


@dataclass(frozen=True)
class ExitAddresses:
    v4: IpAddress
    v6: IpAddress | None


class ExitGuard:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self._transport = transport
        self.home: HomeAddresses | None = None
        self.exit: ExitAddresses | None = None

    def _echo(self, url: str, proxy: str | None, required: bool) -> IpAddress | None:
        try:
            with httpx.Client(proxy=proxy, timeout=15, transport=self._transport, follow_redirects=False) as client:
                return public_ip(client.get(url).text)
        except (httpx.HTTPError, ValueError) as exc:
            if required:
                raise EgressError(f"{'exit' if proxy else 'home'} address check failed: {exc}") from exc
            return None

    def learn_home(self) -> HomeAddresses:
        v4 = self._echo(IPV4_CHECK_URL, None, required=True)
        v6 = self._echo(IPV6_CHECK_URL, None, required=False)
        assert isinstance(v4, ipaddress.IPv4Address)
        self.home = HomeAddresses(v4, ipaddress.IPv6Network(f"{v6}/64", strict=False) if v6 else None)
        self.exit = None
        return self.home

    def check(self, proxy_url: str) -> ExitAddresses:
        if self.home is None:
            raise EgressError("home addresses are unknown")
        seen = ExitAddresses(self._echo(IPV4_CHECK_URL, proxy_url, required=True),  # type: ignore[arg-type]
                             self._echo(IPV6_CHECK_URL, proxy_url, required=False))
        for address in (seen.v4, seen.v6):
            if address is not None and self.home.contains(address):
                raise EgressError("traffic through the exit leaves from the home network")
        if self.exit is None:
            self.exit = seen
        elif seen != self.exit:
            raise EgressError("the exit address changed during the run (use a sticky session)")
        return seen


class Egress(ABC):
    """A private exit for the LinkedIn guest client only."""

    proxy_url: str | None = None

    @property
    @abstractmethod
    def label(self) -> str: ...

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def verify(self) -> ExitAddresses: ...

    @abstractmethod
    def stop(self) -> None: ...


class ProxyEgress(Egress):
    """A (residential) HTTP proxy from config/.env. Its credentials never reach logs or Statistics."""

    def __init__(self, url: str, guard: ExitGuard | None = None):
        self._url = url.strip()
        self._guard = guard or ExitGuard()

    @property
    def label(self) -> str:
        return f"proxy {urlparse(self._url).hostname or '?'}"

    def start(self) -> None:
        scheme = urlparse(self._url).scheme
        if not self._url:
            raise EgressError("no proxy configured (LINKEDIN_PROXY_URL in config/.env)")
        if scheme not in ("http", "https"):
            raise EgressError("the proxy must be http:// or https:// (CONNECT: the proxy resolves DNS, no leak)")
        self._guard.learn_home()
        self.proxy_url = self._url
        try:
            self.verify()
        except EgressError:
            self.stop()
            raise
        log.info("LinkedIn exit ready: %s, differs from the home network", self.label)

    def verify(self) -> ExitAddresses:
        if not self.proxy_url:
            raise EgressError("the exit is not started")
        return self._guard.check(self.proxy_url)

    def stop(self) -> None:
        self.proxy_url = None
