"""Examples from the specification (section D): extracted facts -> code verdict."""

from __future__ import annotations

from datetime import timedelta

import pytest

from vacancy_retriever.scoring.engine import ScoringEngine
from vacancy_retriever.scoring.facts import VacancyFacts

from .conftest import NOW

BELGRADE = {"country": "Serbia", "city": "Belgrade"}
CONFIDENT = {"confidence": "high"}

EXAMPLES = [
    # (id, facts, expected: decision, skip_reason substring, group, stars, permit, english_flag)
    (1, {**BELGRADE, "role_category": "ds_ml", "seniority": "junior", "seniority_source": "title",
         "stack_overlap": "strong", "serbian_entity": "yes", "serbian_entity_evidence": "d.o.o.",
         "employment": "official", "russian_speaking_team": "yes", "employer_type": "ru_with_serbian_entity"},
     ("show", None, 1, 5, "yes", False)),
    (2, {**BELGRADE, "role_category": "manual_wood", "employment": "official", "visa_support": "yes",
         "serbian_entity": "likely", "ad_language": "ru"},
     ("show", None, 2, 5, "yes", False)),
    (3, {**BELGRADE, "role_category": "manual_construction", "employment": "informal",
         "employer_type": "private_person"},
     ("show", None, 4, 4, "no", False)),
    (4, {**BELGRADE, "role_category": "manual_wood", "employment": "informal"},
     ("show", None, 4, 5, "no", False)),
    (5, {**BELGRADE, "role_category": "manual_production", "employment": "unknown", "serbian_entity": "unknown"},
     ("show", None, 2, 4, "likely", False)),
    (6, {**BELGRADE, "role_category": "manual_wood", "ad_language": "sr", "serbian_required": True,
         "required_languages": ["sr"]},
     ("skip", "Ad in Serbian", None, None, None, None)),
    (7, {"role_category": "data_analyst", "seniority": "middle", "seniority_source": "title",
         "work_format": "remote", "employer_type": "russian", "country": "Russia"},
     ("skip", "Grade: middle", None, None, None, None)),
    (8, {"role_category": "data_analyst", "seniority": "junior", "seniority_source": "title", "experience_years": 2,
         "stack_overlap": "strong", "employer_type": "russian", "work_format": "remote", "country": "Russia"},
     ("show", None, 6, 4, "no", False)),  # remote in Russia: group 6 since 15.11.2026 (was 3)
    (9, {"role_category": "python_backend", "seniority": "junior", "seniority_source": "title",
         "employer_type": "russian", "work_format": "remote", "stack_overlap": "strong"},
     ("show", None, 3, 1, "no", False)),
    (10, {"role_category": "nlp_llm", "seniority": "junior", "seniority_source": "title", "country": "Cyprus",
          "employer_type": "intl_russian_speaking", "russian_speaking_team": "yes", "work_format": "remote",
          "stack_overlap": "strong"},
     ("show", None, 3, 3, "no", False)),
    (11, {**BELGRADE, "role_category": "data_engineer", "seniority": "junior", "seniority_source": "title",
          "english_working": True, "russian_speaking_team": "no", "employer_type": "intl_english",
          "serbian_entity": "likely", "work_format": "office", "stack_overlap": "strong"},
     ("show", None, 1, 2, "likely", True)),
    (12, {**BELGRADE, "role_category": "data_analyst", "seniority": "junior", "seniority_source": "title",
          "ad_language": "en", "english_level": "b2", "english_working": False, "russian_speaking_team": "yes",
          "serbian_entity": "yes", "employer_type": "serbian", "employment": "official", "stack_overlap": "strong"},
     ("show", None, 1, 5, "yes", False)),
    (13, {"role_category": "data_labeling", "employer_type": "russian", "work_format": "remote"},
     ("show", None, 3, 3, "no", False)),
    ("14a", {"role_category": "onec", "employer_type": "russian"},
     ("skip", "1C", None, None, None, None)),
    ("14b", {"role_category": "sys_business_analyst", "employer_type": "russian", "work_format": "remote"},
     ("show", None, 3, 1, "no", False)),
    ("15a", {"role_category": "bi_dwh", "seniority": "junior", "seniority_source": "title",
             "employer_type": "russian", "work_format": "remote", "stack_overlap": "strong"},
     ("show", None, 3, 4, "no", False)),
    ("15b", {"role_category": "other_dev", "seniority": "senior", "seniority_source": "title"},
     ("skip", "Other development", None, None, None, None)),
    ("15c", {"role_category": "qa", "seniority": "intern", "seniority_source": "title"},
     ("skip", "QA", None, None, None, None)),
    (16, {**BELGRADE, "role_category": "manual_food", "employment": "official"},
     ("skip", "Kitchen", None, None, None, None)),
    (17, {"role_category": "manual_driver"}, ("skip", "Driver", None, None, None, None)),
    (18, {"role_category": "service"}, ("skip", "Service", None, None, None, None)),
    (19, {"role_category": "office_non_tech"}, ("skip", "Office", None, None, None, None)),
    (20, {"role_category": "other", "confidence": "low"}, ("show", None, 5, 1, None, False)),
    (21, {"role_category": "not_a_vacancy"}, ("hidden", "Not a vacancy", None, None, None, None)),
    (22, {"role_category": "ds_ml", "seniority": "junior", "seniority_source": "title", "work_format": "remote",
          "serbian_entity": "yes", "employment": "official", "employer_type": "ru_with_serbian_entity",
          "country": "Serbia", "stack_overlap": "strong"},
     ("show", None, 1, 5, "yes", False)),
    (23, {**BELGRADE, "role_category": "manual_wood", "ad_language": "sr", "required_languages": ["ru", "sr"],
          "serbian_required": True, "employment": "official"},
     ("show", None, 2, 5, "likely", False)),
    (24, {**BELGRADE, "role_category": "manual_warehouse", "ad_language": "sr", "required_languages": []},
     ("skip", "Ad in Serbian", None, None, None, None)),
    (25, {**BELGRADE, "role_category": "manual_trades", "seniority": "junior", "seniority_source": "label",
          "employment": "official", "experience_years": 2},
     ("show", None, 2, 2, "likely", False)),
    (26, {**BELGRADE, "role_category": "manual_warehouse", "employment": "official"},
     ("show", None, 2, 4, "likely", False)),
]


@pytest.fixture(scope="module")
def engine(config):
    """The user tunes the experience thresholds in Settings; the examples are checked with the 03.11.2026 ones."""
    rules = config.rules.model_copy(deep=True)
    rules.experience_drop.years, rules.experience_drop.years_unless_junior = 3, 2
    return ScoringEngine(rules)


@pytest.mark.parametrize(("example_id", "facts", "expected"), EXAMPLES, ids=[str(e[0]) for e in EXAMPLES])
def test_spec_example(engine, example_id, facts, expected):
    decision, reason_part, group, stars, permit, english = expected
    payload = {**CONFIDENT, **facts}
    verdict = engine.evaluate(VacancyFacts.model_validate(payload), NOW - timedelta(days=1), NOW)

    assert verdict.decision == decision
    if reason_part:
        assert reason_part in (verdict.skip_reason or "")
    if decision == "show":
        assert verdict.skip_reason is None
        assert verdict.group == group
        assert verdict.stars == stars
        if permit:
            assert verdict.residence_permit == permit
        assert verdict.english_flag == english


def test_group_4_is_marked_informal(engine):
    facts = VacancyFacts.model_validate({**CONFIDENT, **BELGRADE, "role_category": "manual_wood",
                                         "employment": "informal"})
    assert engine.evaluate(facts, NOW, NOW).informal_flag


def test_old_vacancy_is_expired(engine):
    facts = VacancyFacts.model_validate({**CONFIDENT, "role_category": "ds_ml"})
    verdict = engine.evaluate(facts, NOW - timedelta(days=31), NOW)
    assert verdict.decision == "skip"
    assert verdict.skip_reason == "Expired"


def test_english_company_outside_serbia_is_dropped(engine):
    facts = VacancyFacts.model_validate({**CONFIDENT, "role_category": "ds_ml", "employer_type": "intl_english",
                                         "english_working": True, "russian_speaking_team": "no",
                                         "country": "Germany"})
    assert engine.evaluate(facts, NOW, NOW).skip_reason == "English-speaking company outside Serbia"


def test_experience_drop_from_the_users_reports(engine):
    """03.11.2026: from 3 years always; from 2 years unless the vacancy is marked junior or intern."""
    def reason(**fields):
        facts = VacancyFacts.model_validate({**CONFIDENT, "role_category": "data_analyst", "employer_type": "russian",
                                             "work_format": "remote", "stack_overlap": "strong", **fields})
        return engine.evaluate(facts, NOW, NOW).skip_reason
    assert reason(experience_years=3) == "Experience: 3+ years"
    assert reason(experience_years=3, seniority="junior", seniority_source="label") == "Experience: 3+ years"
    assert reason(experience_years=2) == "Experience: 2+ years"
    assert reason(experience_years=2, seniority="junior", seniority_source="label") is None
    assert reason(experience_years=1) is None and reason() is None


@pytest.mark.parametrize("title,reason", [
    ("Директор склада", "Grade in the title"), ("Заместитель директора", "Grade in the title"),
    ("Senior Python Developer", "Grade in the title"), ("Product Lead (Web)", "Grade in the title"),
    ("Бизнес-аналитик / Руководитель проектов", "Grade in the title"),
    ("Junior/Senior AI-разработчика", None), ("Member of Technical Staff (Analyst)", None), ("Data Analyst", None),
])
def test_grade_words_in_the_title(engine, title, reason):
    facts = VacancyFacts.model_validate({**CONFIDENT, "title_original": title, "role_category": "data_analyst",
                                         "employer_type": "russian", "work_format": "remote"})
    assert engine.evaluate(facts, NOW, NOW).skip_reason == reason


def test_unknown_enum_values_fall_back_to_defaults():
    facts = VacancyFacts.model_validate({"seniority": "Medior", "employment": "contract??", "confidence": "HIGH",
                                         "required_languages": ["RU", "en-US", ""]})
    assert facts.seniority == "unknown"
    assert facts.employment == "unknown"
    assert facts.confidence == "high"
    assert facts.required_languages == ["en", "ru"]


@pytest.mark.parametrize(("facts", "skipped"), [
    ({"country": "Russia", "city": "Moscow", "work_format": "office"}, False),  # group 7 since 16.11.2026
    ({"country": "Russia", "city": "Kazan", "work_format": "office"}, True),
    ({"country": "France", "work_format": "hybrid"}, True),
    ({"country": "Russia", "work_format": "remote"}, False),
    ({"country": "unknown", "work_format": "office"}, False),
    ({"country": "Serbia", "work_format": "office"}, False),
])
def test_office_outside_serbia_is_dropped(engine, facts, skipped):
    verdict = engine.evaluate(
        VacancyFacts.model_validate({**CONFIDENT, "role_category": "data_analyst", "employer_type": "russian",
                                     **facts}), NOW, NOW)
    assert (verdict.skip_reason == "Office outside Serbia and Moscow") is skipped


def test_russian_ad_is_not_dropped_for_serbian_requirement(engine):
    """TRUSTONE, run 1: the LLM listed only Serbian for an ad written in Russian."""
    facts = VacancyFacts.model_validate({**BELGRADE, "role_category": "manual_trades", "ad_language": "ru",
                                         "required_languages": ["sr"], "serbian_required": True,
                                         "employment": "official"})
    assert engine.evaluate(facts, NOW, NOW).decision == "show"
    facts = facts.model_copy(update={"ad_language": "en"})
    assert engine.evaluate(facts, NOW, NOW).skip_reason == "Serbian required, Russian not required"


def test_english_working_team_gets_at_most_three_stars(engine):
    facts = VacancyFacts.model_validate({**BELGRADE, "role_category": "ds_ml", "seniority": "junior",
                                         "seniority_source": "title", "stack_overlap": "strong",
                                         "english_working": True, "employment": "official", "serbian_entity": "yes"})
    assert engine.evaluate(facts, NOW, NOW).stars == 3


def test_grade_stated_only_in_text_is_dropped(engine):
    facts = VacancyFacts.model_validate({**BELGRADE, "role_category": "ds_ml", "seniority": "senior",
                                         "seniority_source": "text"})
    assert engine.evaluate(facts, NOW, NOW).skip_reason == "Grade: senior"
