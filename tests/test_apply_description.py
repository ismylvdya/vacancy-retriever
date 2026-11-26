"""A letter-only vacancy gets its description on apply, without opening HH or LinkedIn."""

from __future__ import annotations

import httpx
import pytest

from vacancy_retriever.apply.description import LetterDescriptions
from vacancy_retriever.collectors.base import RawItem
from vacancy_retriever.collectors.hh_descriptions import HhDescriptions
from vacancy_retriever.collectors.job_search import JobSearchLookup, ProviderSettings
from vacancy_retriever.config import AppConfig
from vacancy_retriever.storage import Storage
from vacancy_retriever.timeutil import utcnow


@pytest.fixture
def world(tmp_paths, monkeypatch):
    storage = Storage.open(tmp_paths.default_db, tmp_paths.token_log)
    config = AppConfig(tmp_paths)
    seen = []

    def answer(request):
        seen.append(request.url.host)
        if request.url.path == "/usage":
            return httpx.Response(200, json={"data": {"quotas": []}})
        job = {"job_title": "Product Analyst", "employer_name": "Playrix", "job_description": "Полное описание",
               "job_publisher": "Google", "job_apply_link": "https://jobs.example.com/1", "apply_options": []}
        return httpx.Response(200, json={"status": "OK", "data": {"jobs": [job] if world_state["found"] else []}})

    world_state = {"found": True, "mirror": ""}
    lookup = lambda _config: JobSearchLookup([ProviderSettings("jsearch", "k", 200)], tmp_paths.data / "js.json", 40,
                                             transport=httpx.MockTransport(answer), second_query=False)
    monkeypatch.setattr("vacancy_retriever.apply.description.create_lookup", lookup)

    def mirror(request):
        assert request.url.host == 'get-works.ru'
        if world_state['mirror']:
            return httpx.Response(200, text=world_state['mirror'])
        return httpx.Response(404)

    monkeypatch.setattr('vacancy_retriever.apply.description.HhDescriptions',
                        lambda: HhDescriptions(httpx.MockTransport(mirror)))

    def raw(url, detail=None):
        run_id = storage.runs.start({})
        item = RawItem("yandex", url, url, "Product Analyst Playrix Сербия hh.ru — автопоиск", utcnow(), title="Product Analyst",
                       meta={"company": "Playrix", "email": {"parser": "hh_ru"}})
        raw_id = storage.items.insert(run_id, item, "scored")
        if detail:
            storage.items.set_detail(raw_id, detail)
        return storage.items.get(raw_id)

    return LetterDescriptions(config, storage), raw, storage, seen, world_state


def test_found_description_is_kept_and_only_the_api_was_asked(world):
    descriptions, raw, storage, seen, _ = world
    item = raw("https://hh.ru/vacancy/138936181")
    assert item.email_only
    text = descriptions.complete(item)
    assert "Полное описание" in text and "Полное описание" in storage.items.get(item.id).full_text
    assert not storage.items.get(item.id).email_only
    assert set(seen) == {"api.openwebninja.com"}


def test_existing_hh_card_uses_the_exact_copy_on_apply(world):
    descriptions, raw, storage, seen, state = world
    state['mirror'] = '''
        <h2 class="detail-title">Описание вакансии</h2>
        <div class="p-otstup"><p>Задачи аналитика</p></div>
        <a href="https://hh.ru/vacancy/138936181">Источник</a>
    '''
    item = raw('https://hh.ru/vacancy/138936181')
    assert descriptions.complete(item) == item.text + '\n\nЗадачи аналитика'
    assert not storage.items.get(item.id).email_only and seen == []


def test_linkedin_letter_only_asks_the_api(world):
    descriptions, raw, _, seen, state = world
    state["found"] = False
    assert descriptions.complete(raw("https://www.linkedin.com/jobs/view/4455684566/")) is None
    assert set(seen) == {"api.openwebninja.com"}


def test_a_miss_is_tried_once_and_keeps_the_letter(world):
    descriptions, raw, storage, seen, state = world
    state["found"] = False
    item = raw("https://hh.ru/vacancy/1")
    assert descriptions.complete(item) is None
    asked = len(seen)
    assert descriptions.complete(item) is None and len(seen) == asked
    assert storage.items.get(item.id).full_text.startswith("Product Analyst Playrix")


def test_an_item_with_a_description_costs_nothing(world):
    descriptions, raw, _, seen, _state = world
    assert descriptions.complete(raw("https://hh.ru/vacancy/2", detail="Уже есть")) is None and seen == []
