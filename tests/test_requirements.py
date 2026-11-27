"""Experience and grade by place, the IT accreditation signs and the order of categories (user, 27.11.2026)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from vacancy_retriever.collectors.base import RawItem
from vacancy_retriever.collectors.hh_descriptions import parse_page_meta
from vacancy_retriever.collectors.sites.employers import tbank_item
from vacancy_retriever.pipeline.llm_stage import VacancyScorer, parse_scoring_response
from vacancy_retriever.scoring.engine import ScoringEngine
from vacancy_retriever.scoring.facts import VacancyFacts
from vacancy_retriever.scoring.requirements import grade_from_labels, years_from_labels
from vacancy_retriever.scoring.text_facts import TextFacts
from vacancy_retriever.storage import Storage
from vacancy_retriever.storage.items import StoredRawItem
from vacancy_retriever.storage.vacancies import NewVacancy
from vacancy_retriever.timeutil import utcnow

from .conftest import NOW, SAMPLES

RUSSIA_REMOTE = {"role_category": "data_analyst", "country": "Russia", "work_format": "remote",
                 "employer_type": "russian", "stack_overlap": "strong", "confidence": "high"}


def stored(title="Data Analyst", text="", detail=None, multi=False, page_meta=(), **meta) -> StoredRawItem:
    return StoredRawItem(1, 1, False, "src", "1", "", title, text or title, detail, NOW, multi,
                         {"page_meta": list(page_meta), **meta}, "pending_llm", None)


def scored(**fields) -> VacancyFacts:
    """Facts as a reply of the LLM gives them (with places)."""
    return parse_scoring_response({"results": [{"input_id": 1, "vacancies": [fields]}]}, {1})[0].vacancies[0]


@pytest.fixture(scope="module")
def engine(config):
    return ScoringEngine(config.rules)


@pytest.fixture(scope="module")
def text_facts(config):
    return TextFacts(config.rules)


@pytest.mark.parametrize(("lines", "years"), [
    (["Опыт работы: От 1 года до 3 лет"], 1),
    (["Опыт работы: От 3 до 6 лет"], 3),
    (["Опыт работы: Более 6 лет"], 6),
    (["Опыт работы: Нет опыта"], 0),
    (["Теги: Гибрид, Middle, Senior, 1 – 5 лет, Более 5 лет, 5/2"], 1),
    (["Опыт (лет): 2"], 2),
    (["Опыт, лет: 3"], 3),
    (["🟥 Senior DevOps Engineer | 5 year(s) | Senior | IC"], 5),
    (["Грейд: Middle"], None),
    (["Зарплата: 150 000 ₽"], None),
])
def test_years_in_the_page_data(lines, years):
    assert years_from_labels(lines) == years


@pytest.mark.parametrize(("lines", "grade"), [
    (["Теги: Гибрид, Middle, Senior, 1 – 5 лет"], "middle"),
    (["Уровень: Сеньор"], "senior"),
    (["Грейд: Junior, Trainee"], "intern"),
    (["Опыт работы: От 1 года до 3 лет"], None),
])
def test_grade_in_the_page_data(lines, grade):
    assert grade_from_labels(lines) == grade


def test_hh_copy_gives_its_properties_apart_from_the_description():
    html = (SAMPLES / "html" / "hh_mirror" / "job_detail_137701677.html").read_text(encoding="utf-8")
    assert parse_page_meta(html) == ["Опыт работы: От 1 года до 3 лет"]


def test_tbank_tags_are_page_data():
    data = json.loads((SAMPLES / "html" / "tbank" / "getVacancies.json").read_text(encoding="utf-8"))
    item = tbank_item(data["payload"]["vacancies"][0], "tbank")
    assert item.meta["page_meta"] == ["Теги: Гибрид, Middle, Senior, 1 – 5 лет, 5/2"]


def test_the_description_decides_over_the_page_data(engine, text_facts):
    """T-Bank: «1 – 5 лет» in the tags, «от 3 лет» in the description → 3 years, skipped."""
    item = stored(detail="Требования: опыт работы в ML от 3 лет", page_meta=["Теги: Гибрид, Middle, 1 – 5 лет"])
    facts = text_facts.fill(scored(**RUSSIA_REMOTE), item)
    assert (facts.experience_years, facts.experience_source) == (3, "text")
    assert engine.evaluate(facts, NOW, NOW).skip_reason == "Experience: 3+ years"


def test_hh_one_to_three_years_passes_when_the_description_is_silent(engine, text_facts):
    item = stored(detail="Задачи: отчёты. Требования: SQL, Python", page_meta=["Опыт работы: От 1 года до 3 лет"])
    facts = text_facts.fill(scored(**RUSSIA_REMOTE), item)
    assert (facts.experience_years, facts.experience_source) == (1, "label")
    verdict = engine.evaluate(facts, NOW, NOW)
    assert verdict.decision == "show" and verdict.group == 6


def test_the_llm_places_win_over_code_and_the_title_over_the_page(text_facts):
    facts = text_facts.fill(scored(**RUSSIA_REMOTE, experience_by={"title": 1, "label": 3},
                                   grade_by={"title": "junior", "label": ["middle", "senior"]}), stored())
    assert (facts.experience_years, facts.experience_source) == (1, "title")
    assert (facts.seniority, facts.seniority_source) == ("junior", "title")


def test_years_found_anywhere_make_the_grade_irrelevant(engine, text_facts):
    """«Middle» in the tags, «от 1 года» in the description: the years decide, the vacancy stays."""
    facts = text_facts.fill(scored(**RUSSIA_REMOTE, title_original="Middle Data Analyst"),
                            stored(detail="Опыт работы от 1 года", page_meta=["Теги: Middle"]))
    assert facts.seniority == "middle"
    assert engine.evaluate(facts, NOW, NOW).decision == "show"


def test_without_years_the_grade_skips(engine, text_facts):
    facts = text_facts.fill(scored(**RUSSIA_REMOTE), stored(detail="SQL, Python", page_meta=["Теги: Гибрид, Middle"]))
    assert facts.experience_years is None
    assert engine.evaluate(facts, NOW, NOW).skip_reason == "Grade: middle"


def test_grade_counts_with_years_when_the_switch_is_off(config, text_facts):
    rules = config.rules.model_copy(deep=True)
    rules.requirements.years_over_grade = False
    facts = text_facts.fill(scored(**RUSSIA_REMOTE), stored(detail="Опыт работы от 1 года", page_meta=["Теги: Middle"]))
    assert ScoringEngine(rules).evaluate(facts, NOW, NOW).skip_reason == "Grade: middle"


def test_facts_scored_before_places_keep_their_values(text_facts):
    old = VacancyFacts(**RUSSIA_REMOTE, seniority="junior", seniority_source="title", experience_years=1)
    facts = text_facts.fill(old, stored(detail="Опыт работы от 3 лет", page_meta=["Теги: Senior"]))
    assert (facts.seniority, facts.experience_years) == ("junior", 1)


@pytest.mark.parametrize(("fields", "text", "sign"), [
    ({"employer_type": "private_person", "employer_name": "ИП Иванов"}, "", "Employer is a private person"),
    ({"employer_name": "крупный российский банк", "employer_placeholder": True}, "",
     "«крупный российский банк» is not a company name"),
    ({}, "Оформление по ГПХ, удалёнка", "The vacancy mentions «ГПХ»"),
    ({}, "Работа как самозанятый", "The vacancy mentions «самозанятый»"),
    ({}, "Оформление по ТК РФ, ДМС", ""),
])
def test_signs_of_no_it_accreditation(engine, text_facts, fields, text, sign):
    facts = text_facts.fill(scored(**{**RUSSIA_REMOTE, "employer_type": "russian", **fields}), stored(detail=text))
    assert facts.no_accreditation_sign == sign
    verdict = engine.evaluate(facts, NOW, NOW)
    assert (verdict.group == 8) is bool(sign)
    assert (facts.accreditation == "no") is bool(sign)


def test_a_digest_text_gives_no_accreditation_sign(text_facts):
    facts = text_facts.fill(scored(**RUSSIA_REMOTE), stored(text="1) аналитик, ГПХ 2) инженер", multi=True))
    assert facts.no_accreditation_sign == ""


def test_the_llm_reads_the_title_and_the_page_data_apart(config):
    stage = VacancyScorer(config, caller=None, source_names={})  # type: ignore[arg-type]
    item = stored(title="Аналитик данных", text="Аналитик данных\nТеги: Middle\nОписание: SQL",
                  page_meta=["Теги: Middle"])
    [entry] = json.loads(stage.user_prompt([item]))
    assert entry["title"] == "Аналитик данных"
    assert entry["page_meta"] == "Теги: Middle"
    assert "Теги" not in entry["text"]


def test_ml_comes_before_analytics_among_equal_stars(tmp_paths, config):
    storage = Storage.open(tmp_paths.default_db, tmp_paths.token_log)
    run_id = storage.runs.start({})
    engine = ScoringEngine(config.rules)
    for index, (category, published_hours) in enumerate([("data_analyst", 1), ("ds_ml", 5), ("bi_dwh", 2)]):
        published = utcnow() - timedelta(hours=published_hours)
        raw_id = storage.items.insert(run_id, RawItem("src", str(index), f"https://example.com/{index}", "x",
                                                      published), "scored")
        facts = VacancyFacts(**{**RUSSIA_REMOTE, "role_category": category})
        verdict = engine.evaluate(facts, published, utcnow())
        storage.vacancies.insert(run_id, NewVacancy(raw_id, "llm", category, "Acme", category, "unknown",
                                                    verdict.decision, None, published, facts, verdict))
    storage.runs.commit(run_id, {}, 30)
    stars = {r.category: r.stars for r in storage.vacancies.feed()}
    assert stars["ds_ml"] == stars["data_analyst"] == 5
    by_date = [r.category for r in storage.vacancies.feed() if r.stars == 5]
    by_order = [r.category for r in storage.vacancies.feed(config.rules.feed_category_order) if r.stars == 5]
    assert by_date == ["data_analyst", "ds_ml"]
    assert by_order == ["ds_ml", "data_analyst"]
    storage.db.close()
