"""IT accreditation (decision of the user 20.11.2026): register lookup, the yes / no / unknown decision, group 8."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import timedelta

import pytest

from vacancy_retriever.accreditation.names import compact, normalize, short_name, to_cyrillic, valid_inn
from vacancy_retriever.accreditation.registry import Registry, RegistryRecord, RegistryStore
from vacancy_retriever.accreditation.resolver import (
    AccreditationResolver,
    EmployerQuery,
    candidates_for,
    decide,
)
from vacancy_retriever.accreditation.stage import run_accreditation
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.config import AppConfig
from vacancy_retriever.llm.base import LlmProvider, LlmResponse, LlmUsage
from vacancy_retriever.scoring.engine import ScoringEngine
from vacancy_retriever.scoring.facts import VacancyFacts
from vacancy_retriever.storage import Storage
from vacancy_retriever.storage.vacancies import NewVacancy

from vacancy_retriever.timeutil import utcnow

from .conftest import NOW

SBERBANK = RegistryRecord("7707083893", "1", 'ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО "СБЕРБАНК РОССИИ"', "Аннулирована",
                          "2022-07-31")
SBERTECH = RegistryRecord("7736632467", "2", 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "СБЕРТЕХ"', "Действует")
YANDEX = RegistryRecord("7736207543", "3", 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЯНДЕКС"', "Действует")
SBEREGA = RegistryRecord("5000000001", "4", 'ООО "СБЕРЕГАТЕЛЬНЫЕ ТЕХНОЛОГИИ РЕГИОНОВ"', "Действует")


def registry() -> Registry:
    return Registry([SBERBANK, SBERTECH, YANDEX, SBEREGA], "2026-11-03")


def test_names_inn_and_transliteration():
    assert normalize('ООО "СберТех"') == "сбертех" and compact("Сбер Тех") == compact("ООО «СберТех»")
    assert normalize("Публичное акционерное общество «Сбербанк России»") == "сбербанк россии"
    assert short_name('ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЯНДЕКС"') == 'ООО "ЯНДЕКС"'
    assert to_cyrillic("yandex") == "яндекс" and to_cyrillic("ozon") == "озон"
    assert valid_inn("7707083893") and valid_inn("7736207543")
    assert not valid_inn("7707083894") and not valid_inn("123") and not valid_inn("")


def test_register_finds_a_brand_by_prefix_active_first():
    reg = registry()
    names = [r.name for r in reg.with_prefix("сбер")]
    assert names[0] == SBERTECH.name and SBERBANK.name in names
    assert reg.find_inn("7736207543") == YANDEX and reg.find_name("ООО Яндекс") == [YANDEX]
    found = candidates_for(reg, EmployerQuery("Yandex"), None)
    assert [c["inn"] for c in found] == [YANDEX.inn] and found[0]["register"] == "active"


def test_decision_yes_no_unknown():
    reg = registry()
    # Сбер: the bank is not accredited, its IT company is → yes (the user can be hired by СберТех)
    status, note = decide({"identified": "high", "russian": True, "entities": [
        {"name": "ПАО Сбербанк", "inn": SBERBANK.inn}, {"name": "ООО СберТех", "inn": SBERTECH.inn}]}, reg)
    assert status == "yes" and "СБЕРТЕХ" in note
    # only the annulled bank, the LLM says the group has no other IT entity → no
    bank = {"identified": "high", "russian": True, "group_complete": True, "entities": [{"inn": SBERBANK.inn}]}
    assert decide(bank, reg)[0] == "no"
    # …but a big group may have an unlisted IT subsidiary (VK: «Одноклассники» annulled, ООО «ВК» active) → unknown
    assert decide({**bank, "group_complete": False}, reg)[0] == "unknown"
    assert decide({k: v for k, v in bank.items() if k != "group_complete"}, reg)[0] == "unknown", "old cache rows"
    # a real INN that the register does not know, company surely identified, the whole group → no
    absent = {"identified": "high", "russian": True, "group_complete": True,
              "entities": [{"name": "ООО Ромашка", "inn": "7710140679"}]}
    assert decide(absent, reg)[0] == "no"
    # the same but the LLM is not sure which company it is → unknown
    assert decide({**absent, "identified": "medium"}, reg)[0] == "unknown"
    # an invented INN (bad check digit) is no proof of anything → unknown
    made_up = {"identified": "high", "russian": True, "group_complete": True,
               "entities": [{"name": "ООО Ромашка", "inn": "7710140678"}]}
    assert decide(made_up, reg)[0] == "unknown"
    # nothing known at all
    assert decide({}, reg)[0] == "unknown"
    # found by name only
    assert decide({"identified": "low", "entities": [{"name": "ООО «Яндекс»", "inn": ""}]}, reg)[0] == "yes"


def test_engine_moves_a_surely_unaccredited_russian_vacancy_to_group_8(config):
    engine = ScoringEngine(config.rules)
    base = dict(title_original="Junior Data Analyst", role_category="data_analyst", seniority="junior",
                stack_overlap="strong", country="Russia", city="Москва", work_format="office",
                employer_type="russian", confidence="high", ad_language="ru")
    assert engine.evaluate(VacancyFacts(**base), None, NOW).group == 7
    assert engine.evaluate(VacancyFacts(**base, it_accreditation="unknown"), None, NOW).group == 7
    assert engine.evaluate(VacancyFacts(**base, it_accreditation="yes"), None, NOW).group == 7
    verdict = engine.evaluate(VacancyFacts(**base, it_accreditation="no"), None, NOW)
    assert (verdict.group, verdict.decision) == (8, "show")
    remote = {**base, "work_format": "remote", "city": "unknown", "it_accreditation": "no"}
    assert engine.evaluate(VacancyFacts(**remote), None, NOW).group == 8
    serbia = {**base, "country": "Serbia", "city": "Belgrade", "it_accreditation": "no"}
    assert engine.evaluate(VacancyFacts(**serbia), None, NOW).group == 1, "only the Russian groups are checked"
    assert config.rules.groups[8].collapsed and config.rules.ordered_groups[-1] == 8


class AnswerProvider(LlmProvider):
    """Answers the accreditation prompt from a dict: employer → answer."""

    name = "fake"

    def __init__(self, answers: dict[str, dict]):
        self.answers = answers
        self.inputs: list[list[dict]] = []

    async def complete_json(self, system, user, model, schema=None) -> LlmResponse:
        payload = json.loads(user)
        self.inputs.append(payload)
        results = [{"input_id": e["input_id"], **self.answers.get(e["employer"], {"identified": "low"})}
                   for e in payload]
        data = {"results": results}
        return LlmResponse(data, LlmUsage(10, 5, 0, 0, model), json.dumps(data), "none")


@pytest.fixture
def env(tmp_paths):
    config = AppConfig(tmp_paths)
    storage = Storage.open(tmp_paths.default_db, tmp_paths.token_log)
    folder = tmp_paths.data / "it_registry"
    folder.mkdir()
    records = [{"inn": r.inn, "ogrn": r.ogrn, "name": r.name, "status": r.status} for r in registry().records]
    (folder / "2026-11-03.json").write_text(json.dumps({"snapshot_date": "2026-11-03", "records": records}))
    (folder / "state.json").write_text(json.dumps({"checked_at": utcnow().isoformat()}))  # no download in tests
    yield config, storage
    storage.db.close()


def add_vacancy(storage, employer: str, **extra) -> int:
    facts = VacancyFacts(title_original="Junior Data Analyst", role_category="data_analyst", seniority="junior",
                         stack_overlap="strong", country="Russia", city="Москва", work_format="office",
                         employer_type="russian", confidence="high", ad_language="ru", employer_name=employer, **extra)
    run_id = storage.runs.start({})
    raw = storage.db.execute(
        "INSERT INTO raw_items(run_id, staged, source_id, external_id, text, collected_at, status)"
        " VALUES (?,0,'s',?,?,?,'scored')", (run_id, f"x{employer}", f"Вакансия {employer}", NOW.isoformat())).lastrowid
    vacancy = NewVacancy(raw, "llm", "Junior Data Analyst", employer, "data_analyst", "junior", "show", None, None,
                         facts, ScoringEngine(AppConfig().rules).evaluate(facts, None, NOW))
    vacancy_id = storage.vacancies.insert(run_id, vacancy)
    storage.runs.commit(run_id, {}, 3650, now=NOW)
    return vacancy_id


def test_stage_marks_the_feed_and_moves_no_accreditation_to_group_8(env):
    config, storage = env
    sber = add_vacancy(storage, "Сбер")
    romashka = add_vacancy(storage, "Ромашка")
    nobody = add_vacancy(storage, "Неизвестное ООО")
    no_name = add_vacancy(storage, "unknown")
    provider = AnswerProvider({
        "Сбер": {"identified": "high", "russian_company": True, "matching_candidates": ["c1", "c2"],
                 "legal_entities": [{"name": "ПАО Сбербанк", "inn": "7707083893"}], "note": "Сбербанк"},
        "Ромашка": {"identified": "high", "russian_company": True, "matching_candidates": [],
                    "legal_entities": [{"name": "ООО Ромашка", "inn": "7710140679"}],
                    "other_it_entities_possible": False},
    })
    result = run_accreditation(config, storage, lambda: provider, PoliteHttpClient(0, 0), threading.Event())
    assert result.problem is None and result.employers_asked == 3 and len(provider.inputs) == 1
    sent = {e["employer"]: e for e in provider.inputs[0]}
    assert {c["name"] for c in sent["Сбер"]["candidates"]} >= {'ООО "СБЕРТЕХ"'}, "register candidates are offered"
    assert all("source" not in c for c in sent["Сбер"]["candidates"])
    state = {v: storage.vacancies.get(v) for v in (sber, romashka, nobody, no_name)}
    assert state[sber].facts.it_accreditation == "yes" and state[sber].group_no == 7
    assert state[romashka].facts.it_accreditation == "no" and state[romashka].group_no == 8
    assert state[romashka].user_status == "new", "group 8 is in the feed, folded"
    assert state[nobody].facts.it_accreditation == "unknown" and state[nobody].group_no == 7
    assert state[no_name].facts.it_accreditation == "unknown"
    # cached: a second pass asks nothing; «unknown» is asked again only after recheck_unknown_days
    again = AnswerProvider({})
    assert run_accreditation(config, storage, lambda: again, PoliteHttpClient(0, 0), threading.Event()).employers_asked == 0
    assert again.inputs == []


def test_a_new_register_snapshot_redecides_without_llm(env):
    config, storage = env
    romashka = add_vacancy(storage, "Ромашка")
    provider = AnswerProvider({"Ромашка": {"identified": "high", "russian_company": True,
                                           "legal_entities": [{"name": "ООО Ромашка", "inn": "7710140679"}],
                    "other_it_entities_possible": False}})
    run_accreditation(config, storage, lambda: provider, PoliteHttpClient(0, 0), threading.Event())
    assert storage.vacancies.get(romashka).group_no == 8
    folder = config.paths.data / "it_registry"
    records = json.loads((folder / "2026-11-03.json").read_text())["records"]
    records.append({"inn": "7710140679", "ogrn": "9", "name": 'ООО "РОМАШКА"', "status": "Действует"})
    (folder / "2026-11-20.json").write_text(json.dumps({"snapshot_date": "2026-11-20", "records": records}))
    silent = AnswerProvider({})
    run_accreditation(config, storage, lambda: silent, PoliteHttpClient(0, 0), threading.Event())
    assert silent.inputs == [] and storage.vacancies.get(romashka).facts.it_accreditation == "yes"
    assert storage.vacancies.get(romashka).group_no == 7


def test_register_refresh_downloads_only_a_newer_snapshot(tmp_path):
    import httpx

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/data"):
            return httpx.Response(200, json=[{"name": "2022-09-25.json", "download_url": "https://raw.test/old"},
                                             {"name": "2026-11-03.json", "download_url": "https://raw.test/new"},
                                             {"name": "README.md", "download_url": "https://raw.test/readme"}])
        return httpx.Response(200, json={"snapshot_date": "2026-11-03", "records": []})

    http = PoliteHttpClient(0, 0, transport=httpx.MockTransport(handler))
    store = RegistryStore(tmp_path, "https://api.test/repos/x/contents/data", 7)
    assert store.refresh(http, NOW) is None and (tmp_path / "2026-11-03.json").exists()
    assert calls == ["https://api.test/repos/x/contents/data", "https://raw.test/new"]
    assert store.refresh(http, NOW + timedelta(days=1)) is None and len(calls) == 2, "not due yet"
    assert store.refresh(http, NOW + timedelta(days=8)) is None and len(calls) == 3, "listing only, nothing newer"
    assert store.load().snapshot_date == "2026-11-03"


def test_feed_shows_accreditation_tags(env, monkeypatch):
    from vacancy_retriever.web.views import CardBuilder

    config, storage = env
    vacancy = storage.vacancies.get(add_vacancy(storage, "Сбер", it_accreditation="yes",
                                                it_accreditation_note='ООО "СБЕРТЕХ" (ИНН 7736632467) — в реестре'))
    flags = CardBuilder(config, storage).build(vacancy).flags
    accredited = next(f for f in flags if f == "IT accredited")
    assert accredited.kind == "ok" and "СБЕРТЕХ" in accredited.title
    unchecked = storage.vacancies.get(add_vacancy(storage, "Кто-то"))
    flag = next(f for f in CardBuilder(config, storage).build(unchecked).flags if f == "IT accreditation not checked")
    assert flag.kind == "plain" and "Not checked yet" in flag.title


def test_no_llm_without_a_register(tmp_paths):
    config = AppConfig(tmp_paths)
    storage = Storage.open(tmp_paths.default_db, tmp_paths.token_log)
    add_vacancy(storage, "Сбер")
    provider = AnswerProvider({})
    result = run_accreditation(config, storage, lambda: provider, PoliteHttpClient(0, 0), threading.Event())
    assert provider.inputs == [] and result.problem
    storage.db.close()


def test_resolver_keeps_employers_missing_from_the_reply_for_later(env):
    config, storage = env

    async def ask(system, user, n):
        return {"results": []}

    resolver = AccreditationResolver(config, storage.accreditation, registry(), ask)
    assert asyncio.run(resolver.resolve([EmployerQuery("Сбер")])) == []
    assert storage.accreditation.get_many(["сбер"]) == {}


def test_the_employer_inn_from_the_source_needs_no_llm(env):
    """trudvsem gives the employer's INN: active in the register → «yes» at once; otherwise the usual check by name."""
    config, storage = env
    tech = add_vacancy(storage, "АО Сбертех")
    bank = add_vacancy(storage, "ПАО Сбербанк")
    storage.items.set_meta(storage.vacancies.get(tech).primary_raw_id, "employer_inn", SBERTECH.inn)
    storage.items.set_meta(storage.vacancies.get(bank).primary_raw_id, "employer_inn", SBERBANK.inn)
    provider = AnswerProvider({"ПАО Сбербанк": {"identified": "high", "russian_company": True,
                                                "matching_candidates": [], "legal_entities": []}})
    run_accreditation(config, storage, lambda: provider, PoliteHttpClient(0, 0), threading.Event())
    assert storage.vacancies.get(tech).facts.it_accreditation == "yes"
    assert "ИНН из вакансии" in storage.vacancies.get(tech).facts.it_accreditation_note
    assert [e["employer"] for batch in provider.inputs for e in batch] == ["ПАО Сбербанк"], \
        "an annulled INN is not «no» by itself: the group may have an accredited IT company"


def test_entities_named_without_an_inn_are_found_by_name(env, monkeypatch):
    """The LLM knows «ООО СберТех» but not its INN: the register by the exact name gives it → yes."""
    config, storage = env
    vacancy = add_vacancy(storage, "СберЛизинг")
    provider = AnswerProvider({"СберЛизинг": {"identified": "high", "russian_company": True,
                                              "legal_entities": [{"name": 'ООО "СБЕРТЕХ"', "inn": ""}],
                                              "other_it_entities_possible": True}})
    run_accreditation(config, storage, lambda: provider, PoliteHttpClient(0, 0), threading.Event())
    facts = storage.vacancies.get(vacancy).facts
    assert facts.it_accreditation == "yes" and SBERTECH.inn in facts.it_accreditation_note


def test_no_is_blocked_by_an_active_entity_named_like_the_brand(env):
    """VK: the LLM listed only the annulled «Одноклассники» and said «nothing else» — ООО «ВК» is active → unknown."""
    config, storage = env
    folder = config.paths.data / "it_registry"
    data = json.loads((folder / "2026-11-03.json").read_text())
    data["records"] += [{"inn": "7743001840", "ogrn": "7", "name": 'ООО "ВК"', "status": "Действует"},
                        {"inn": "7705773443", "ogrn": "8", "name": 'ООО "ОДНОКЛАССНИКИ"', "status": "Аннулирована"}]
    (folder / "2026-11-03.json").write_text(json.dumps(data))
    vk = add_vacancy(storage, "VK / Одноклассники")
    provider = AnswerProvider({"VK / Одноклассники": {
        "identified": "high", "russian_company": True, "other_it_entities_possible": False,
        "legal_entities": [{"name": "ООО «Одноклассники»", "inn": "7705773443"}]}})
    run_accreditation(config, storage, lambda: provider, PoliteHttpClient(0, 0), threading.Event())
    facts = storage.vacancies.get(vk).facts
    assert facts.it_accreditation == "unknown" and "7743001840" in facts.it_accreditation_note


def test_no_russian_speaking_tag_in_russian_groups(env):
    """Groups 3 / 6 / 7 / 8 mean a Russian employer: the «Russian-speaking» tag there is noise (user 20.11.2026)."""
    config, storage = env
    from vacancy_retriever.web.views import CardBuilder

    vacancy = storage.vacancies.get(add_vacancy(storage, "Сбер"))
    assert vacancy.group_no == 7 and vacancy.facts.russian_link
    assert "Russian-speaking" not in CardBuilder(config, storage).build(vacancy).flags
    vacancy.group_no = 1
    assert "Russian-speaking" in CardBuilder(config, storage).build(vacancy).flags


def test_a_sign_in_the_vacancy_needs_neither_the_register_nor_the_llm(env):
    """ГПХ, an «ИП …» employer or a placeholder name (rules.yaml → no_it_accreditation_signs, 27.11.2026)."""
    config, storage = env
    placeholder = add_vacancy(storage, "крупный российский банк", employer_placeholder=True,
                              no_accreditation_sign="«крупный российский банк» is not a company name")
    provider = AnswerProvider({})
    result = run_accreditation(config, storage, lambda: provider, PoliteHttpClient(0, 0), threading.Event())
    assert result.employers_asked == 0 and provider.inputs == []
    record = storage.vacancies.get(placeholder)
    assert record.group_no == 8 and record.facts.accreditation == "no"
    assert record.facts.it_accreditation is None, "the register was not asked"
