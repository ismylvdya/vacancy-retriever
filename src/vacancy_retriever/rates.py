"""Exchange rates to RUB, one file per day in data/rates/YYYY-MM-DD.json ({"USD": 80.5, "RSD": 0.77, …, "RUB": 1}).

Fetched once per run day (Central Bank of Russia daily rates, fallback open.er-api.com). Salaries are converted by
the rates of the day the vacancy was collected, so recalculation never shifts old cards with today's rates.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from .collectors.http import PoliteHttpClient
from .paths import ProjectPaths

log = logging.getLogger(__name__)

CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
FALLBACK_URL = "https://open.er-api.com/v6/latest/RUB"


class RatesStore:
    def __init__(self, paths: ProjectPaths):
        self._folder = paths.data / "rates"

    def _path(self, day: date) -> Path:
        return self._folder / f"{day.isoformat()}.json"

    def for_date(self, day: date | None) -> dict[str, float] | None:
        """Rates of that day, else of the closest earlier day, else the earliest known; None if none saved."""
        files = sorted(self._folder.glob("*.json"))
        if not files:
            return None
        chosen = files[0]
        if day is not None:
            earlier = [f for f in files if f.stem <= day.isoformat()]
            chosen = earlier[-1] if earlier else files[0]
        try:
            return json.loads(chosen.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def fetch_today(self, http: PoliteHttpClient, today: date) -> dict[str, float] | None:
        path = self._path(today)
        if path.exists():
            return self.for_date(today)
        rates = self._download(http)
        if rates is None:
            return self.for_date(today)
        self._folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rates, indent=1, sort_keys=True), encoding="utf-8")
        return rates

    @staticmethod
    def _download(http: PoliteHttpClient) -> dict[str, float] | None:
        try:
            data = json.loads(http.get_text(CBR_URL))
            rates = {code: v["Value"] / v["Nominal"] for code, v in data["Valute"].items()}
        except Exception as exc:  # noqa: BLE001 - rates are never worth failing a run
            log.info("CBR rates failed (%s), trying the fallback", exc)
            try:
                data = json.loads(http.get_text(FALLBACK_URL))
                rates = {code: 1 / value for code, value in data["rates"].items() if value}
            except Exception as exc2:  # noqa: BLE001
                log.info("Fallback rates failed: %s", exc2)
                return None
        rates["RUB"] = 1.0
        return rates
