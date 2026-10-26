"""Salary text → numbers, the RUB amount on cards and the "salary floor too high" rule."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vacancy_retriever.scoring.engine import ScoringEngine
from vacancy_retriever.scoring.facts import VacancyFacts
from vacancy_retriever.scoring.salary import Salary, monthly_rub, parse_salary

RATES = {"RUB": 1.0, "USD": 80.0, "EUR": 90.0, "RSD": 0.77, "KZT": 0.18, "GBP": 107.0}
NOW = datetime(2026, 10, 26, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize("text, expected", [
    ("60k – 120k RUB/month", Salary(60000, 120000, "RUB", "month")),
    ("до 100k RUB/month", Salary(None, 100000, "RUB", "month")),
    ("110 000 RSD/мес", Salary(110000, None, "RSD", "month")),
    ("от 80 000 ₽/мес", Salary(80000, None, "RUB", "month")),
    ("$120–180K/year", Salary(120000, 180000, "USD", "year")),
    ("€5500–8000 gross/месяц", Salary(5500, 8000, "EUR", "month")),
    ("50–60 тыс. ₽", Salary(50000, 60000, "RUB", "month")),
    ("600 000 KZT", Salary(600000, None, "KZT", "month")),
    ("65,000–95,000 EUR", Salary(65000, 95000, "EUR", "year")),
    ("до 1200 ₽ за час", Salary(None, 1200, "RUB", "hour")),
    ("от 3500 до 4000 $ за месяц", Salary(3500, 4000, "USD", "month")),
    ("от 45K", Salary(45000, None, None, "month")),
])
def test_parse_salary(text, expected):
    assert parse_salary(text) == expected


@pytest.mark.parametrize("text", ["По договорённости", "10% от продаж, 12 месяцев", "", None])
def test_no_amount(text):
    assert parse_salary(text) is None


def test_monthly_rub():
    assert monthly_rub(120000, parse_salary("$120–180K/year"), RATES) == 800000
    assert monthly_rub(1200, parse_salary("до 1200 ₽ за час"), RATES) == 192000
    assert monthly_rub(45000, parse_salary("от 45K"), RATES) is None  # no currency → no conversion


@pytest.fixture(scope="module")
def engine(config):
    return ScoringEngine(config.rules)


def facts(salary: str) -> VacancyFacts:
    return VacancyFacts.model_validate({"role_category": "data_analyst", "seniority": "junior", "seniority_source": "title",
                                        "country": "Russia", "work_format": "remote", "employer_type": "russian",
                                        "stack_overlap": "strong", "confidence": "high", "salary_text": salary})


def test_high_salary_floor_is_dropped(engine):
    assert engine.evaluate(facts("от 3500 до 4000 $ за месяц"), NOW, NOW, RATES).skip_reason == "Salary floor above 175 000 ₽"
    assert engine.evaluate(facts("от 120000 до 200000 ₽ за месяц"), NOW, NOW, RATES).decision == "show"
    assert engine.evaluate(facts("до 300 000 ₽"), NOW, NOW, RATES).decision == "show", "no floor — no drop"
    assert engine.evaluate(facts("от 3500 $"), NOW, NOW, None).decision == "show", "no rates — no drop"
