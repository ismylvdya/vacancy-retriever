"""DaData «suggest party»: Russian legal entities by name (INN, OKVED, address). Optional — needs DADATA_API_KEY in
config/.env (free registration at dadata.ru; tariffs and limits — on their site). Without the key the resolver
works from the register and the LLM only, and is more often unsure.
Only the employer's name from a vacancy is sent (never anything about the user)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .. import traffic

log = logging.getLogger(__name__)

SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"


@dataclass(frozen=True)
class Party:
    inn: str
    name: str
    okved: str = ""
    city: str = ""
    active: bool = True


class DaData:
    def __init__(self, key: str, timeout: float = 15.0, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            headers={"Authorization": f"Token {key}", "Accept": "application/json", "Content-Type": "application/json"},
            timeout=timeout, transport=transport)
        self.failed: str | None = None  # the first refusal (bad key, limit): no more requests in this run

    def close(self) -> None:
        self._client.close()

    def suggest(self, name: str, count: int = 5) -> list[Party]:
        if self.failed or not name.strip():
            return []
        try:
            response = self._client.post(SUGGEST_URL, json={"query": name[:300], "count": count, "type": "LEGAL"})
            traffic.add(traffic.response_size(response))
        except httpx.HTTPError as exc:
            log.warning("DaData: %s", exc)
            return []
        if response.status_code in (401, 403, 429):
            self.failed = f"DaData refused: HTTP {response.status_code}"
            log.warning(self.failed)
            return []
        if response.status_code != 200:
            return []
        parties = []
        for entry in (response.json() or {}).get("suggestions") or []:
            data = entry.get("data") or {}
            if not data.get("inn"):
                continue
            parties.append(Party(
                inn=str(data["inn"]),
                name=str(((data.get("name") or {}).get("short_with_opf")) or entry.get("value") or ""),
                okved=str(data.get("okved") or ""),
                city=str(((data.get("address") or {}).get("data") or {}).get("city") or ""),
                active=((data.get("state") or {}).get("status") or "ACTIVE") == "ACTIVE",
            ))
        return parties
