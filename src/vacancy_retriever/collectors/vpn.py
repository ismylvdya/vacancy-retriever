"""A VPN tunnel only for the LinkedIn guest fetcher: Xray (Homebrew) with ONE server of the user's VLESS subscription.

Decision 2026-10-27: LinkedIn pages must never be requested from the home IP. The tunnel lives only while the
details stage reads LinkedIn pages, listens on 127.0.0.1 only and nothing else in the app (or on the Mac) uses it.
Before EVERY LinkedIn page `verify()` checks that Xray is alive and that the exit IP seen through the tunnel is not
the home IP and has not changed since the tunnel started; any failure → VpnError → LinkedIn is not opened again in
this run. The subscription URL (a secret) comes from config/.env; the Xray config with the server key is written to
data/xray/ with owner-only permissions and deleted when the tunnel stops.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx

log = logging.getLogger(__name__)

IP_CHECK_URL = "https://api.ipify.org"
XRAY_CANDIDATES = ("/opt/homebrew/bin/xray", "/usr/local/bin/xray")


class VpnError(Exception):
    """The tunnel is not usable: LinkedIn must not be opened."""


@dataclass(frozen=True)
class VlessServer:
    name: str
    uuid: str
    host: str
    port: int
    params: dict[str, str]


def parse_subscription(body: str) -> list[VlessServer]:
    """Base64 (or plain) list of vless:// links → servers. Other protocols are ignored."""
    text = body.strip()
    if "://" not in text:
        try:
            text = base64.b64decode(text + "=" * (-len(text) % 4)).decode("utf-8", "replace")
        except ValueError as exc:
            raise VpnError("subscription is neither a list of links nor base64") from exc
    servers: list[VlessServer] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("vless://"):
            continue
        url = urlparse(line)
        if not url.hostname or not url.port or not url.username:
            continue
        params = {key: values[0] for key, values in parse_qs(url.query).items()}
        servers.append(VlessServer(unquote(url.fragment).strip(), unquote(url.username), url.hostname, url.port, params))
    return servers


def choose_server(servers: list[VlessServer], wanted: str) -> VlessServer:
    """The first server whose name contains `wanted` (case-insensitive) — always the same one, never a fallback."""
    for server in servers:
        if wanted.lower() in server.name.lower():
            return server
    raise VpnError(f"server «{wanted}» is not in the subscription any more — choose another one in config/mail.yaml")


def xray_config(server: VlessServer, port: int) -> dict:
    p = server.params
    network = p.get("type", "tcp")
    security = p.get("security", "none")
    stream: dict = {"network": network, "security": security}
    if security == "reality":
        stream["realitySettings"] = {"serverName": p.get("sni", ""), "fingerprint": p.get("fp", "chrome"),
                                     "publicKey": p.get("pbk", ""), "shortId": p.get("sid", ""),
                                     "spiderX": unquote(p.get("spx", ""))}
    elif security == "tls":
        stream["tlsSettings"] = {"serverName": p.get("sni", ""), "fingerprint": p.get("fp", "chrome")}
    if network == "grpc":
        stream["grpcSettings"] = {"serviceName": p.get("serviceName", "")}
    elif network in ("ws", "httpupgrade", "xhttp"):
        key = {"ws": "wsSettings", "httpupgrade": "httpupgradeSettings", "xhttp": "xhttpSettings"}[network]
        stream[key] = {"path": unquote(p.get("path", "/")), "host": p.get("host", "")}
    user = {"id": server.uuid, "encryption": p.get("encryption", "none")}
    if p.get("flow"):
        user["flow"] = p["flow"]
    return {
        "log": {"loglevel": "warning"},
        # Plain HTTP proxy on localhost: the client sends CONNECT with the host name, so DNS goes through the tunnel too.
        "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "http", "settings": {}}],
        "outbounds": [{"protocol": "vless", "settings": {"vnext": [{"address": server.host, "port": server.port,
                                                                    "users": [user]}]},
                       "streamSettings": stream}],
    }


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _public_ip(text: str) -> str:
    value = text.strip()
    address = ipaddress.ip_address(value)  # raises ValueError for anything that is not an IP
    if address.is_private or address.is_loopback:
        raise ValueError(f"not a public address: {value}")
    return value


class XrayTunnel:
    def __init__(self, subscription_url: str, server_name: str, work_dir: Path, xray_path: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        self._subscription_url = subscription_url
        self._server_name = server_name
        self._work_dir = work_dir
        self._xray = xray_path or next((p for p in XRAY_CANDIDATES if Path(p).exists()), None) or shutil.which("xray")
        self._transport = transport
        self._process: subprocess.Popen | None = None
        self._config_path = work_dir / "config.json"
        self.proxy_url: str | None = None
        self.home_ip: str | None = None
        self.exit_ip: str | None = None
        self.server: str | None = None

    def start(self) -> None:
        if not self._subscription_url:
            raise VpnError("no VPN subscription: set LINKEDIN_VPN_SUBSCRIPTION in config/.env")
        if not self._xray:
            raise VpnError("Xray is not installed (brew install xray)")
        try:
            with httpx.Client(timeout=20, transport=self._transport) as client:
                body = client.get(self._subscription_url.split("happ://add/", 1)[-1]).text
                self.home_ip = _public_ip(client.get(IP_CHECK_URL).text)
        except (httpx.HTTPError, ValueError) as exc:
            raise VpnError(f"subscription or IP check unavailable: {exc}") from exc
        server = choose_server(parse_subscription(body), self._server_name)
        self.server = server.name
        port = _free_port()
        self._work_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(xray_config(server, port), handle)
        log_file = (self._work_dir / "xray.log").open("ab")
        self._process = subprocess.Popen([self._xray, "run", "-c", str(self._config_path)], stdout=log_file,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self.proxy_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self.stop()
                raise VpnError("Xray exited right after start (see data/xray/xray.log)")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            self.stop()
            raise VpnError("Xray did not open its local port")
        self.exit_ip = None
        self.verify()
        log.info("VPN for LinkedIn is up: server «%s», exit IP differs from home", self.server)

    def verify(self) -> str:
        """Before every LinkedIn page: Xray alive, exit IP reachable, not the home IP, unchanged since start."""
        if self._process is None or self._process.poll() is not None:
            raise VpnError("Xray is not running")
        try:
            with httpx.Client(proxy=self.proxy_url, timeout=15, transport=self._transport) as client:
                ip = _public_ip(client.get(IP_CHECK_URL).text)
        except (httpx.HTTPError, ValueError) as exc:
            raise VpnError(f"VPN does not answer: {exc}") from exc
        if ip == self.home_ip:
            raise VpnError("traffic through the VPN leaves from the home IP")
        if self.exit_ip is None:
            self.exit_ip = ip
        elif ip != self.exit_ip:
            raise VpnError("the VPN exit IP changed during the run")
        return ip

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None
        self.proxy_url = None
        try:
            self._config_path.unlink()
        except FileNotFoundError:
            pass
