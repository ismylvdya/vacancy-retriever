from __future__ import annotations

import logging
import time
from pathlib import Path

from .collectors.http import PoliteHttpClient, is_forbidden
from .config import SourceConfig
from .paths import ProjectPaths

log = logging.getLogger(__name__)

REFRESH_SECONDS = 30 * 24 * 3600
EXTENSIONS = (".png", ".svg", ".ico", ".jpg", ".jpeg", ".webp", ".gif")
_MAGIC = {b"\x89PNG": ".png", b"\xff\xd8\xff": ".jpg", b"GIF8": ".gif", b"RIFF": ".webp", b"\x00\x00\x01\x00": ".ico"}


def _guess_extension(content: bytes, url: str) -> str:
    for magic, ext in _MAGIC.items():
        if content.startswith(magic):
            return ext
    if b"<svg" in content[:500]:
        return ".svg"
    suffix = Path(url.split("?")[0]).suffix.lower()
    return suffix if suffix in EXTENSIONS else ".ico"


class IconStore:
    """Source icons: user override in config/icons/ > downloaded copy in data/icons/."""

    def __init__(self, paths: ProjectPaths):
        self._paths = paths

    def user_icon(self, source: SourceConfig) -> Path | None:
        if source.icon:
            path = self._paths.user_icons / source.icon
            if path.exists():
                return path
        return self._first_existing(self._paths.user_icons, source.id)

    def downloaded(self, source_id: str) -> Path | None:
        return self._first_existing(self._paths.icons, source_id)

    def resolve(self, source: SourceConfig) -> Path | None:
        return self.user_icon(source) or self.downloaded(source.id)

    def needs_refresh(self, source_id: str) -> bool:
        path = self.downloaded(source_id)
        return path is None or time.time() - path.stat().st_mtime > REFRESH_SECONDS

    def refresh(self, source: SourceConfig, url: str | None, http: PoliteHttpClient) -> None:
        if not url or is_forbidden(url) or self.user_icon(source) or not self.needs_refresh(source.id):
            return
        if url.startswith("//"):
            url = "https:" + url
        try:
            content = http.get_bytes(url)
        except Exception as exc:  # noqa: BLE001 - an icon is never worth failing a source
            log.info("Icon download for %s failed: %s", source.id, exc)
            return
        if not content or len(content) > 512_000:
            return
        self._paths.icons.mkdir(parents=True, exist_ok=True)
        for old in self._paths.icons.glob(f"{source.id}.*"):
            old.unlink()
        (self._paths.icons / f"{source.id}{_guess_extension(content, url)}").write_bytes(content)

    @staticmethod
    def _first_existing(folder: Path, stem: str) -> Path | None:
        for ext in EXTENSIONS:
            path = folder / f"{stem}{ext}"
            if path.exists():
                return path
        return None
