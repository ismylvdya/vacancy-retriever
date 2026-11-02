"""Decisions of 02.11.2026: an ad in Russian gets a star more all else equal; a letter alone gets at most 4 stars."""

from __future__ import annotations

from vacancy_retriever.scoring.engine import ScoringEngine
from vacancy_retriever.scoring.facts import VacancyFacts

from .conftest import NOW

BASE = {"country": "Serbia", "city": "Belgrade", "role_category": "ds_ml", "stack_overlap": "strong",
        "employment": "official", "confidence": "high"}


def stars(config, email_only=False, **facts):
    verdict = ScoringEngine(config.rules).evaluate(VacancyFacts(**{**BASE, **facts}), None, NOW, None, email_only)
    return verdict.stars


def test_russian_ad_gets_a_star_more_all_else_equal(config):
    assert stars(config, stack_overlap="partial", ad_language="en") == 4
    assert stars(config, stack_overlap="partial", ad_language="ru") == 5
    assert stars(config, stack_overlap="none", ad_language="ru") == stars(config, stack_overlap="none", ad_language="en") + 1


def test_russian_ad_never_goes_above_the_cap(config):
    assert stars(config, ad_language="ru") == 5


def test_email_only_gets_at_most_four(config):
    assert stars(config) == 5
    assert stars(config, email_only=True) == 4
    assert stars(config, email_only=True, ad_language="ru") == 4
    assert stars(config, email_only=True, stack_overlap="none") == 3, "a lower score stays as it is"


def test_explain_shows_both(config):
    lines = ScoringEngine(config.rules).explain(VacancyFacts(**BASE, ad_language="ru"), None, NOW, None, True)
    fit = next(line for line in lines if line.startswith("Fit"))
    assert "+1 ad in Russian" in fit and "E-mail only cap 4" in fit
