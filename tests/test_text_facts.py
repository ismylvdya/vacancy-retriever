"""Facts found by code in the text (decisions of 03.11.2026, from the user's reports)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from vacancy_retriever.scoring.engine import ScoringEngine
from vacancy_retriever.scoring.facts import VacancyFacts
from vacancy_retriever.scoring.text_facts import experience_from_text, fill_from_text

from .conftest import NOW


@pytest.mark.parametrize("text,years", [
    ("Professional Experience Required - At least 2 to 4 years of proven experience in a relevant field", 2),
    ("3 years of experience in Data Analyst.", 3),
    ("Have 3+ years of experience in Data Science or a related field", 3),
    ("2-8 years of engineering experience in high-velocity teams", 2),
    ("A minimum of 4 years in software development with a strong focus on Python", 4),
    ("2–4 years of experience in risk management", 2),
    ("Experience: 5 years", 5),
    ("Опыт работы в роли Product/Data Analyst от 3 лет", 3),
    ("Требуется 2+ года опыта", 2),
    ("Iskustvo od 3 godine u analizi", 3),
    ("1–2 years of experience. 5 years of Python experience", 1),
    ("For over 25 years, we have partnered with banks", None),
    ("Your data is kept for up to 2 years in our candidate pool", None),
    ("We have 30 years of experience in the market", None),
    ("Компании 10 лет, офис в Белграде", None),
    ("Data Analyst, SQL, Python", None),
])
def test_years_of_experience(text, years):
    assert experience_from_text(text) == years


def item(text="", detail=None, multi=False, page_meta=()):
    return SimpleNamespace(text=text, detail_text=detail, multi=multi, page_meta=list(page_meta),
                           description=detail or text)


def test_text_fills_only_what_the_llm_left_empty(config):
    facts = fill_from_text(VacancyFacts(), config.rules, item("x", "3 years of experience required"))
    assert facts.experience_years == 3
    facts = fill_from_text(VacancyFacts(experience_years=1), config.rules, item("3 years of experience"))
    assert facts.experience_years == 1, "the LLM's number stays"


def test_a_post_with_several_vacancies_is_left_alone(config):
    facts = fill_from_text(VacancyFacts(), config.rules, item("3 years of experience, Russian", multi=True))
    assert facts.experience_years is None and not facts.russian_mention


@pytest.mark.parametrize("fields,text,russian,english", [
    ({"required_languages": ["ru", "en"]}, "", True, False),
    ({"ad_language": "ru"}, "", True, False),
    ({"russian_speaking_team": "yes", "employer_type": "intl_english"}, "", True, False),
    ({"employer_type": "intl_russian_speaking"}, "", True, False),
    ({"employer_type": "intl_english"}, "Our founders are from Russia", True, False),
    ({}, "Знание русского языка", True, False),
    ({}, "Product Analyst / Playrix / Сербия / hh.ru — автопоиск", True, False),
    ({"required_languages": ["en"]}, "", False, True),
    ({"employer_type": "intl_english"}, "We are based in Boston", False, True),
    ({"english_working": True}, "", False, True),
    ({"employer_type": "serbian", "ad_language": "en"}, "Beograd", False, False),
    ({}, "Prussia, Cisco", False, False),
])
def test_language_tags(config, fields, text, russian, english):
    facts = fill_from_text(VacancyFacts(**fields), config.rules, item(text))
    assert (facts.russian_link, facts.english_team) == (russian, english)


def stars(config, text="", **fields):
    base = {"country": "Serbia", "city": "Belgrade", "role_category": "ds_ml", "stack_overlap": "strong",
            "employment": "official", "confidence": "high"}
    facts = fill_from_text(VacancyFacts(**{**base, **fields}), config.rules, item(text))
    return ScoringEngine(config.rules).evaluate(facts, None, NOW).stars


def test_international_company_without_a_link_to_russian_gets_at_most_three(config):
    assert stars(config, employer_type="intl_english") == 3, "BETBY: english_working was not set, it had 4"
    assert stars(config, employer_type="intl_english", russian_speaking_team="yes") == 5
    assert stars(config, "Russian is a plus", employer_type="intl_english") == 5
    assert stars(config, employer_type="serbian") == 5
