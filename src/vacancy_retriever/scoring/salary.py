"""Salary text from the LLM facts ("80k–90k RUB/мес", "€5500–8000 gross/месяц", "$120–180K/year") → numbers.

Pure code, no LLM. Used for the RUB amount next to the original on cards and for the rule "drop a vacancy whose
monthly salary floor is above N ₽" (rules.yaml → salary). Unknown currency → no conversion and no drop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CURRENCIES = [  # (code, markers) — checked in order; markers are lowercase
    ("RUB", ("₽", "руб", "rub", "rur", "р/мес")),
    ("USD", ("$", "usd", "долл")),
    ("EUR", ("€", "eur", "евро")),
    ("GBP", ("£", "gbp")),
    ("RSD", ("rsd", "din", "дин", "рсд")),
    ("KZT", ("₸", "kzt", "тенге", "тнг")),
    ("BYN", ("byn", "бел. руб", "бел.руб")),
    ("UAH", ("₴", "uah", "грн")),
    ("GEL", ("₾", "gel", "лари")),
    ("AMD", ("֏", "amd", "драм")),
    ("PLN", ("zł", "pln", "злот")),
    ("TRY", ("₺", "try")),
    ("AED", ("aed", "дирхам")),
    ("CHF", ("chf",)),
]
HOUR = re.compile(r"(в|за|/)\s*час|/\s*ч\b|per\s+hour|/\s*h(our)?\b|hourly", re.I)
YEAR = re.compile(r"(в|за|/)\s*год|year|/\s*yr|annual|per\s+annum|p\.a\.", re.I)
MONTH = re.compile(r"мес|month|/\s*mo\b|monthly", re.I)
# A number with optional thousands separators and a multiplier: "100 000", "120,000", "1.5k", "60К", "50 тыс.".
# The multiplier must not start a word: "600 000 KZT" is not 600 000 thousand.
NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:[\s\u00a0\u202f,.]\d{3})+|\d+(?:[.,]\d+)?)\s*"
                    r"(k(?![a-z])|к(?![а-я])|тыс\.?|млн|m(?![a-z]))?", re.I)
RANGE_GAP = re.compile(r"^\s*(?:[-–—]|до|to)\s*$", re.I)
ANNUAL_IF_ABOVE = 15000  # EUR / USD / GBP without a period above this are yearly ("65,000–95,000 EUR")


@dataclass(frozen=True)
class Salary:
    low: float | None
    high: float | None
    currency: str | None
    period: str  # month | year | hour


def _currency(text: str) -> str | None:
    lowered = text.lower()
    for code, markers in CURRENCIES:
        if any(marker in lowered for marker in markers):
            return code
    return None


def _number(raw: str, multiplier: str | None) -> float:
    digits = raw.replace(" ", " ").replace(" ", " ")
    if re.fullmatch(r"\d{1,3}(?:[\s,.]\d{3})+", digits):
        value = float(re.sub(r"[\s,.]", "", digits))
    else:
        value = float(digits.replace(",", ".").replace(" ", ""))
    factor = {"k": 1e3, "к": 1e3, "тыс": 1e3, "тыс.": 1e3, "млн": 1e6, "m": 1e6}.get((multiplier or "").lower(), 1)
    return value * factor


def parse_salary(text: str | None) -> Salary | None:
    if not text or not any(ch.isdigit() for ch in text):
        return None
    tokens: list[tuple[float, bool, int, int]] = []  # (value, has multiplier, start, end)
    for match in NUMBER.finditer(text):
        if text[match.end():match.end() + 2].strip().startswith("%"):
            continue
        tokens.append((_number(match.group(1), match.group(2)), bool(match.group(2)), match.start(), match.end()))
    if not tokens:
        return None
    first = tokens[0]
    low: float | None = first[0]
    high: float | None = None
    if len(tokens) > 1:
        gap = re.sub(r"[$€£₽]", "", text[first[3]:tokens[1][2]])
        if RANGE_GAP.match(gap):
            high = tokens[1][0]
            if tokens[1][1] and not first[1] and low and low * 1000 <= high * 1.001:
                low *= 1000  # "$120–180K", "50–60 тыс.": the multiplier of the upper bound applies to both
    if (low or 0) < 100 and (high or 0) < 100:
        return None  # "10% от продаж, 12 месяцев" — no amount at all
    if low is not None and low < 100:
        low = None
    before = text[:first[2]].lower()
    if re.search(r"(^|\s)(до|up to|max)\s*[$€£₽]?\s*$", before):
        low, high = None, low
    currency = _currency(text)
    if HOUR.search(text):
        period = "hour"
    elif YEAR.search(text):
        period = "year"
    elif MONTH.search(text):
        period = "month"
    elif currency in ("EUR", "USD", "GBP") and (low or high or 0) >= ANNUAL_IF_ABOVE:
        period = "year"
    else:
        period = "month"
    return Salary(low, high, currency, period)


def monthly_rub(amount: float | None, salary: Salary, rates: dict[str, float], hours_per_month: int = 160) -> float | None:
    """Amount → RUB per month by `rates` (RUB per one unit). None if the currency or its rate is unknown."""
    if amount is None or not salary.currency or salary.currency not in rates:
        return None
    per_month = {"month": amount, "year": amount / 12, "hour": amount * hours_per_month}[salary.period]
    return per_month * rates[salary.currency]


def format_rub(value: float) -> str:
    return f"{round(value / 1000) * 1000:,.0f}".replace(",", " ") + " ₽"
