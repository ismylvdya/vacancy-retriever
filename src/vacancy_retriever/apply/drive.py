from __future__ import annotations

import json
import os
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..config import DriveSettings
from .models import ApplyError

FOLDER_MIME = "application/vnd.google-apps.folder"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"


@dataclass
class DriveItem:
    id: str
    url: str


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}?usp=sharing"


def file_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"


class CloudFolders(ABC):
    """Where a vacancy's folder and its CV live. `reason` says why it is unavailable (shown in the sidebar)."""

    reason: str = ""

    @property
    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def ensure_folder(self, key: int, name: str) -> DriveItem: ...

    @abstractmethod
    def upload_pdf(self, key: int, folder_id: str, path: Path) -> DriveItem: ...


class NoDrive(CloudFolders):
    def __init__(self, reason: str):
        self.reason = reason

    @property
    def available(self) -> bool:
        return False

    def ensure_folder(self, key: int, name: str) -> DriveItem:
        raise ApplyError(self.reason, kind="setup")

    def upload_pdf(self, key: int, folder_id: str, path: Path) -> DriveItem:
        raise ApplyError(self.reason, kind="setup")


class GoogleToken:
    """OAuth tokens of the Desktop-app client in data/google_drive_token.json (owner-only permissions)."""

    def __init__(self, path: Path, client_id: str, client_secret: str):
        self.path = path
        self.client_id = client_id
        self.client_secret = client_secret

    @property
    def exists(self) -> bool:
        return self.path.exists()

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(self.path, 0o600)

    def access_token(self, client: httpx.Client) -> str:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("access_token") and data.get("expires_at", 0) > time.time() + 60:
            return data["access_token"]
        response = client.post(TOKEN_URL, data={
            "grant_type": "refresh_token", "refresh_token": data.get("refresh_token", ""),
            "client_id": self.client_id, "client_secret": self.client_secret})
        if response.status_code in (400, 401):
            raise ApplyError("Google Drive login expired: run `vacancy-retriever drive-login`", kind="setup")
        response.raise_for_status()
        fresh = response.json()
        data.update(access_token=fresh["access_token"], expires_at=time.time() + int(fresh.get("expires_in", 3600)))
        self.save(data)
        return data["access_token"]


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDrive(CloudFolders):
    """Drive API v3 with the drive.file scope: the app sees only folders and files it created itself.

    Every folder / file carries appProperties (vr_root, vr_vacancy, vr_pdf), so after a crash between "created"
    and "saved to the database" the same item is found again instead of being created twice."""

    def __init__(self, token: GoogleToken, settings: DriveSettings, transport: httpx.BaseTransport | None = None):
        self._token = token
        self._settings = settings
        self._client = httpx.Client(timeout=60, transport=transport, follow_redirects=False)

    @property
    def available(self) -> bool:
        return self._token.exists

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token.access_token(self._client)}"}

    def _call(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, url, headers={**self._headers(), **kwargs.pop("headers", {})},
                                            **kwargs)
        except httpx.HTTPError as exc:
            raise ApplyError(f"Google Drive: {exc}") from exc
        if response.status_code >= 400:
            raise ApplyError(f"Google Drive HTTP {response.status_code}: {response.text[:200]}")
        return response.json() if response.content else {}

    def _find(self, query: str) -> str | None:
        found = self._call("GET", f"{API}/files", params={"q": f"{query} and trashed = false",
                                                           "fields": "files(id)", "spaces": "drive"})
        files = found.get("files") or []
        return files[0]["id"] if files else None

    def _create_folder(self, name: str, parent: str | None, props: dict[str, str]) -> str:
        body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME, "appProperties": props}
        if parent:
            body["parents"] = [parent]
        return self._call("POST", f"{API}/files", params={"fields": "id"}, json=body)["id"]

    def _root(self) -> str:
        found = self._find(f"appProperties has {{ key='vr_root' and value='1' }} and mimeType = '{FOLDER_MIME}'")
        return found or self._create_folder(self._settings.root_folder, None, {"vr_root": "1"})

    def ensure_folder(self, key: int, name: str) -> DriveItem:
        folder_id = self._find(f"appProperties has {{ key='vr_vacancy' and value='{key}' }}"
                               f" and mimeType = '{FOLDER_MIME}'")
        if folder_id is None:
            folder_id = self._create_folder(name, self._root(), {"vr_vacancy": str(key)})
        if self._settings.share_with_link:
            self._call("POST", f"{API}/files/{folder_id}/permissions", json={"type": "anyone", "role": "reader"})
        return DriveItem(folder_id, folder_url(folder_id))

    def upload_pdf(self, key: int, folder_id: str, path: Path) -> DriveItem:
        content = path.read_bytes()
        file_id = self._find(f"appProperties has {{ key='vr_pdf' and value='{key}' }} and '{_quote(folder_id)}' in parents")
        if file_id:
            self._call("PATCH", f"{UPLOAD}/files/{file_id}", params={"uploadType": "media", "fields": "id"},
                       content=content, headers={"Content-Type": "application/pdf"})
            return DriveItem(file_id, file_url(file_id))
        boundary = f"vr{uuid.uuid4().hex}"
        meta = json.dumps({"name": path.name, "parents": [folder_id], "appProperties": {"vr_pdf": str(key)}})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: application/pdf\r\n\r\n").encode() + content + f"\r\n--{boundary}--".encode()
        created = self._call("POST", f"{UPLOAD}/files", params={"uploadType": "multipart", "fields": "id"},
                             content=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        return DriveItem(created["id"], file_url(created["id"]))
