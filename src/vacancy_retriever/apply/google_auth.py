from __future__ import annotations

import base64
import hashlib
import secrets
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .drive import TOKEN_URL, GoogleToken

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
SCOPE = "https://www.googleapis.com/auth/drive.file"
# Read-only names / dates / types of all Drive files, never their content — only for a one-off import of the old
# applications (`drive-login --metadata`); a plain `drive-login` gives the minimal access back.
METADATA_SCOPE = "https://www.googleapis.com/auth/drive.metadata.readonly"


def login(token: GoogleToken, open_browser: Callable[[str], object] = webbrowser.open, timeout: int = 300,
          metadata: bool = False) -> None:
    """One-time consent in the browser (loopback redirect + PKCE); the refresh token is saved next to the data."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    received: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            if query.get("state") == state:
                received.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("Vacancy Retriever: Google Drive connected, you can close this tab.".encode())

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 5
    redirect = f"http://127.0.0.1:{server.server_port}"
    open_browser(f"{AUTH_URL}?" + urlencode({
        "client_id": token.client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": f"{SCOPE} {METADATA_SCOPE}" if metadata else SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"}))
    deadline = time.monotonic() + timeout
    while not received and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()
    if "code" not in received:
        raise RuntimeError(received.get("error") or "No answer from Google (timed out)")
    response = httpx.post(TOKEN_URL, data={
        "grant_type": "authorization_code", "code": received["code"], "redirect_uri": redirect,
        "client_id": token.client_id, "client_secret": token.client_secret, "code_verifier": verifier}, timeout=60)
    response.raise_for_status()
    data = response.json()
    if not data.get("refresh_token"):
        raise RuntimeError("Google returned no refresh token")
    token.save({"refresh_token": data["refresh_token"], "access_token": data.get("access_token"),
                "expires_at": time.time() + int(data.get("expires_in", 3600))})
