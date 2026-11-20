"""Internship programmes / recruitment waves from ANY source (user 20.11.2026, docs/PROGRAMMES.md): one rule for
posts, letters and sites; never dropped by title keywords; never lost when two LLM calls disagree."""

from __future__ import annotations

import json
import threading
from datetime import timedelta
from typing import ClassVar

import pytest

from vacancy_retriever.collectors.base import CollectContext, Collector, CollectResult, RawItem
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.registry import register
from vacancy_retriever.config import AppConfig, SourceConfig, SourcesFile
from vacancy_retriever.llm.base import LlmProvider, LlmResponse, LlmUsage
from vacancy_retriever.pipeline.prefilter import Prefilter
from vacancy_retriever.pipeline.progress import ProgressTracker
from vacancy_retriever.pipeline.runner import Pipeline, RunOptions, programme_safety_net
from vacancy_retriever.scoring.facts import VacancyFacts
from vacancy_retriever.storage import Storage
from vacancy_retriever.storage.items import StoredRawItem

from .conftest import NOW


def stored(title: str, **meta) -> StoredRawItem:
    return StoredRawItem(1, 1, True, "src", "1", "", title, title, None, NOW - timedelta(days=1), False, meta,
                         "pending_llm", None)


@pytest.mark.parametrize("title", ["Стажировка в Т-Банке: Java, аналитика, QA", "Internship programme: Frontend & ML",
                                   "Открыт набор в школу аналитиков: Java-трек"])
def test_programme_titles_are_never_dropped_by_keywords(config, title):
    assert Prefilter(config.rules).check(stored(title), NOW).action == "keep"


def test_role_titles_are_still_dropped(config):
    assert Prefilter(config.rules).check(stored("Junior Java Developer"), NOW).action == "skip"
    assert Prefilter(config.rules).check(stored("1С-программист"), NOW).action == "skip"


def test_kind_from_the_llm_and_defaults():
    assert VacancyFacts(kind="programme").kind == "programme"
    assert VacancyFacts(kind="school").kind == "vacancy", "an unknown value falls back to the default"


def test_safety_net_keeps_a_wave_letter_the_scoring_call_rejected():
    letter = stored("Стажировка Mindbox", letter_kind="wave", email={"subject": "[Стажировка Mindbox] Бизнес зовёт"})
    records = [VacancyFacts(role_category="not_a_vacancy"), VacancyFacts(role_category="not_a_vacancy")]
    programme_safety_net(letter, records)
    assert len(records) == 1 and records[0].kind == "programme" and records[0].role_category == "other"
    assert records[0].title_original.startswith("[Стажировка Mindbox]") and records[0].confidence == "low"
    # nothing to fix: a real record, or not a wave letter
    ok = [VacancyFacts(role_category="data_analyst", kind="programme")]
    programme_safety_net(letter, ok)
    assert ok[0].role_category == "data_analyst"
    plain = [VacancyFacts(role_category="not_a_vacancy")]
    programme_safety_net(stored("Новости"), plain)
    assert plain[0].role_category == "not_a_vacancy"


# ---------------------------------------------------------------- a Telegram-like post through the whole run

@register
class FakePostCollector(Collector):
    type_name = "fakepost"
    posts: ClassVar[list[str]] = []

    def collect(self, ctx: CollectContext) -> CollectResult:
        return CollectResult(items=[RawItem(self.source.id, str(i), f"https://telegram.me/x/{i}", text, NOW,
                                            multi=True) for i, text in enumerate(FakePostCollector.posts, 1)])


class ProgrammeProvider(LlmProvider):
    name = "fake"

    def __init__(self) -> None:
        self.payloads: list[list[dict]] = []

    async def complete_json(self, system, user, model, schema=None) -> LlmResponse:
        payload = json.loads(user)
        self.payloads.append(payload)
        results = [{"input_id": e["input_id"], "vacancies": [{
            "kind": "programme", "title_original": "Стажировка в Т-Банке: аналитика", "role_category": "data_analyst",
            "seniority": "intern", "seniority_source": "title", "stack_overlap": "strong", "country": "Russia",
            "city": "Moscow", "work_format": "hybrid", "employer_type": "russian", "confidence": "high",
            "ad_language": "ru", "summary_ru": "Набор до 15 марта"}]} for e in payload]
        data = {"results": results}
        return LlmResponse(data, LlmUsage(10, 5, 0, 0, model), json.dumps(data), "none")


def test_a_programme_post_from_any_channel_reaches_the_feed_with_the_tag(tmp_paths):
    config = AppConfig(tmp_paths)
    config.__dict__["sources"] = SourcesFile(sources=[SourceConfig(
        id="tbank_education", type="fakepost", name="Т-Образование", params={"source_employer": "Т-Банк"})])
    storage = Storage.open(tmp_paths.default_db, tmp_paths.token_log)
    FakePostCollector.posts = ["Открыт набор на стажировку в Т-Банке: аналитика. Заявки до 15 марта"]
    provider = ProgrammeProvider()
    pipeline = Pipeline(config, storage, lambda: provider, ProgressTracker(tmp_paths.data / "progress.json"),
                        threading.Event(), http_factory=lambda: PoliteHttpClient(0, 0), now=lambda: NOW)
    assert pipeline.run(RunOptions(notify=False)).status == "committed"
    [card] = storage.vacancies.feed()
    assert card.facts.kind == "programme" and card.facts.wave
    assert provider.payloads[0][0]["hints"]["source_employer"] == "Т-Банк"
    storage.db.close()
