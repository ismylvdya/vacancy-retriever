from __future__ import annotations

import asyncio
import json
import threading
from datetime import timedelta
from typing import Any, ClassVar

import pytest

from vacancy_retriever.collectors.base import (
    CollectContext,
    Collector,
    CollectResult,
    RawItem,
    SourceCursor,
    SourceNotConfigured,
)
from vacancy_retriever.collectors.registry import _REGISTRY, register
from vacancy_retriever.config import AppConfig, SourceConfig, SourcesFile
from vacancy_retriever.llm.base import LlmError, LlmProvider, LlmResponse, LlmUsage
from vacancy_retriever.pipeline.progress import ProgressTracker
from vacancy_retriever.pipeline.runner import Pipeline, RunOptions
from vacancy_retriever.storage import Storage

from .conftest import NOW

FEED: dict[str, list[RawItem]] = {}
FLAGGED: list[dict[str, Any]] = []


@register
class FakeCollector(Collector):
    type_name = "fake"
    calls: ClassVar[list[SourceCursor]] = []

    def collect(self, ctx: CollectContext) -> CollectResult:
        FakeCollector.calls.append(ctx.cursor)
        last = int(ctx.cursor.last_id or 0)
        items = [i for i in FEED.get(self.source.id, []) if int(i.external_id) > last]
        newest = str(max([int(i.external_id) for i in items], default=last)) if items or last else None
        return CollectResult(items=items, new_cursor=SourceCursor(last_id=newest))

    def after_commit(self, processed_meta):
        FLAGGED.extend(processed_meta)
        return []


@register
class BrokenCollector(Collector):
    type_name = "broken"

    def collect(self, ctx: CollectContext) -> CollectResult:
        raise RuntimeError("site changed its layout")


@register
class UnconfiguredCollector(Collector):
    type_name = "unconfigured"

    def collect(self, ctx: CollectContext) -> CollectResult:
        raise SourceNotConfigured("no credentials")


def post(source: str, post_id: int, text: str, **meta: Any) -> RawItem:
    return RawItem(source, str(post_id), f"https://telegram.me/{source}/{post_id}", text,
                   NOW - timedelta(hours=post_id % 24 + 1), multi=True, meta=meta)


def facts(title: str, employer: str, category: str = "ds_ml", **extra: Any) -> dict[str, Any]:
    base = {"title_original": title, "role_category": category, "seniority": "junior", "seniority_source": "title",
            "stack_overlap": "strong", "employer_name": employer, "employer_type": "ru_with_serbian_entity",
            "serbian_entity": "yes", "employment": "official", "country": "Serbia", "city": "Belgrade",
            "confidence": "high", "summary_ru": "Модели"}
    return {**base, **extra}


class FakeProvider(LlmProvider):
    """Reads `FACTS:{json list}` from each input text."""

    name = "fake"

    def __init__(self, fail: bool = False, block: threading.Event | None = None, same_pairs: bool = True):
        self.fail = fail
        self.block = block
        self.same_pairs = same_pairs
        self.calls = 0

    async def complete_json(self, system: str, user: str, model: str, schema=None) -> LlmResponse:
        self.calls += 1
        if self.block is not None:
            self.block.set()
            await asyncio.sleep(30)
        if self.fail:
            raise LlmError("usage limit reached", kind="limit", retryable=False)
        payload = json.loads(user)
        if payload and "pair_id" in payload[0]:
            data = {"results": [{"pair_id": p["pair_id"], "same": self.same_pairs} for p in payload]}
        else:
            results = []
            for entry in payload:
                marker = entry["text"].split("FACTS:", 1)
                vacancies = json.loads(marker[1]) if len(marker) > 1 else [{"role_category": "not_a_vacancy"}]
                results.append({"input_id": entry["input_id"], "vacancies": vacancies})
            data = {"results": results}
        return LlmResponse(data, LlmUsage(100, 50, 0, 0, model), json.dumps(data), "none")


@pytest.fixture
def env(tmp_paths):
    config = AppConfig(tmp_paths)
    sources = [
        SourceConfig(id="chan_a", type="fake", name="Channel A"),
        SourceConfig(id="chan_b", type="fake", name="Channel B"),
        SourceConfig(id="broken", type="broken", name="Broken site"),
        SourceConfig(id="mail", type="unconfigured", name="Mail"),
        SourceConfig(id="off", type="fake", name="Off", enabled=False),
    ]
    config.__dict__["sources"] = SourcesFile(sources=sources)
    storage = Storage.open(tmp_paths.default_db, tmp_paths.token_log)
    FEED.clear()
    FLAGGED.clear()
    FakeCollector.calls.clear()
    yield config, storage, tmp_paths
    storage.db.close()


def make_pipeline(config, storage, paths, provider: LlmProvider, cancel: threading.Event | None = None) -> Pipeline:
    from vacancy_retriever.collectors.http import PoliteHttpClient

    return Pipeline(config, storage, lambda: provider, ProgressTracker(paths.data / "progress.json"),
                    cancel or threading.Event(), http_factory=lambda: PoliteHttpClient(0, 0), now=lambda: NOW)


def run(config, storage, paths, provider, **kw):
    return make_pipeline(config, storage, paths, provider).run(RunOptions(notify=False, **kw))


def test_full_run_commits_feed_health_and_tokens(env):
    config, storage, paths = env
    FEED["chan_a"] = [
        post("chan_a", 1, "FACTS:" + json.dumps([facts("Junior Data Scientist", "Acme d.o.o.")])),
        post("chan_a", 2, "FACTS:" + json.dumps([facts("Бариста", "Cafe", "service")])),
        post("chan_a", 3, "Сдаю квартиру"),
    ]
    summary = run(config, storage, paths, FakeProvider())

    assert summary.status == "committed"
    assert summary.new_by_group == {1: 1}
    feed = storage.vacancies.feed()
    assert [v.title for v in feed] == ["Junior Data Scientist"]
    assert feed[0].stars == 5 and feed[0].residence_permit == "yes"
    assert feed[0].sources[0].url == "https://telegram.me/chan_a/1"
    auto = storage.vacancies.processed("auto_skipped")
    assert [v.skip_reason for v in auto] == ["Service industry"]

    health = {r["source_id"]: r for r in storage.runs.source_runs(summary.run_id)}
    assert health["chan_a"]["status"] == "ok" and health["chan_a"]["collected"] == 3
    assert health["chan_a"]["to_feed"] == 1
    assert health["broken"]["status"] == "error" and "layout" in health["broken"]["error"]
    assert health["mail"]["status"] == "not_configured"
    assert health["off"]["status"] == "disabled"
    assert summary.errors == 1

    calls = storage.llm_calls.by_run(summary.run_id)
    assert calls and calls[0]["input_tokens"] == 100
    assert paths.token_log.read_text().count("\n") == len(calls)
    assert storage.runs.cursor("chan_a").last_id == "3"


def test_second_run_takes_only_new_and_keeps_processed_out(env):
    config, storage, paths = env
    FEED["chan_a"] = [post("chan_a", 1, "FACTS:" + json.dumps([facts("Junior ML Engineer", "Acme")]))]
    run(config, storage, paths, FakeProvider())
    vacancy = storage.vacancies.feed()[0]
    storage.vacancies.set_user_status(vacancy.id, "applied")

    FEED["chan_a"].append(post("chan_a", 2, "FACTS:" + json.dumps([facts("Junior ML Engineer", "Acme")])))
    provider = FakeProvider()
    summary = run(config, storage, paths, provider)

    assert FakeCollector.calls[-2].last_id == "1"
    assert summary.new_total == 0
    assert storage.vacancies.feed() == []
    applied = storage.vacancies.get(vacancy.id)
    assert applied.user_status == "applied"
    assert {s.url for s in applied.sources} == {"https://telegram.me/chan_a/1", "https://telegram.me/chan_a/2"}


def test_repost_in_other_channel_is_not_sent_to_llm(env):
    config, storage, paths = env
    text = "Junior Data Analyst в Белграде. " * 10 + "FACTS:" + json.dumps([facts("Junior Data Analyst", "Beta")])
    FEED["chan_a"] = [post("chan_a", 1, text)]
    FEED["chan_b"] = [post("chan_b", 7, text + " ")]
    provider = FakeProvider()
    run(config, storage, paths, provider)

    feed = storage.vacancies.feed()
    assert len(feed) == 1
    assert {s.source_id for s in feed[0].sources} == {"chan_a", "chan_b"}
    assert provider.calls == 1


def test_llm_failure_keeps_items_pending_and_retries_next_run(env):
    config, storage, paths = env
    FEED["chan_a"] = [post("chan_a", 1, "FACTS:" + json.dumps([facts("Junior CV Engineer", "Gamma", "cv")]))]
    summary = run(config, storage, paths, FakeProvider(fail=True))

    assert summary.status == "committed"
    assert summary.pending_llm == 1
    assert "usage limit" in (summary.llm_error or "")
    assert storage.runs.cursor("chan_a").last_id == "1"
    assert storage.vacancies.feed() == []

    summary = run(config, storage, paths, FakeProvider())
    assert summary.pending_llm == 0
    assert [v.title for v in storage.vacancies.feed()] == ["Junior CV Engineer"]


def test_cancel_during_llm_rolls_back_everything_but_tokens(env):
    config, storage, paths = env
    FEED["chan_a"] = [post("chan_a", 1, "FACTS:" + json.dumps([facts("Junior Data Scientist", "Acme")]))]
    started = threading.Event()
    cancel = threading.Event()
    pipeline = make_pipeline(config, storage, paths, FakeProvider(block=started), cancel)
    result: dict[str, Any] = {}
    worker = threading.Thread(target=lambda: result.setdefault("summary", pipeline.run(RunOptions(notify=False))))
    worker.start()
    assert started.wait(10)
    cancel.set()
    worker.join(15)

    summary = result["summary"]
    assert summary.status == "cancelled"
    assert storage.runs.get(summary.run_id)["status"] == "cancelled"
    assert storage.db.query("SELECT COUNT(*) AS n FROM raw_items")[0]["n"] == 0
    assert storage.db.query("SELECT COUNT(*) AS n FROM vacancies")[0]["n"] == 0
    assert storage.db.query("SELECT COUNT(*) AS n FROM source_runs")[0]["n"] == 0
    assert not storage.runs.has_cursor("chan_a")
    calls = storage.llm_calls.by_run(summary.run_id)
    assert calls and all(c["cancelled"] == 1 and c["tokens_unknown"] == 1 for c in calls)


def test_interrupted_run_is_recovered_as_cancelled(env):
    config, storage, paths = env
    run_id = storage.runs.start({})
    storage.items.insert(run_id, post("chan_a", 5, "x"), "pending_llm")
    assert storage.runs.recover_interrupted() == [run_id]
    assert storage.runs.get(run_id)["status"] == "cancelled"
    assert storage.db.query("SELECT COUNT(*) AS n FROM raw_items")[0]["n"] == 0


def test_digest_post_becomes_several_vacancies(env):
    config, storage, paths = env
    digest = [facts("Junior BI Analyst", "Delta", "bi_dwh", work_format="remote", employer_type="russian",
                    serbian_entity="no", country="Russia", employment="unknown"),
              facts("Senior Go Developer", "Delta", "other_dev"),
              facts("QA intern", "Delta", "qa")]
    FEED["chan_a"] = [post("chan_a", 1, "Digest FACTS:" + json.dumps(digest))]
    summary = run(config, storage, paths, FakeProvider())
    assert summary.new_by_group == {3: 1}
    assert len(storage.vacancies.processed("auto_skipped")) == 2


def test_gray_zone_duplicates_are_merged_by_llm(env):
    config, storage, paths = env
    FEED["chan_a"] = [post("chan_a", 1, "FACTS:" + json.dumps([facts("Data Scientist (junior)", "Acme Labs")]))]
    FEED["chan_b"] = [post("chan_b", 2, "FACTS:" + json.dumps([facts("Junior Data Scientist / ML", "Acme Labs d.o.o.")]))]
    run(config, storage, paths, FakeProvider(same_pairs=True))
    feed = storage.vacancies.feed()
    assert len(feed) == 1
    assert {s.source_id for s in feed[0].sources} == {"chan_a", "chan_b"}


def test_expired_new_vacancies_leave_feed_on_commit(env):
    config, storage, paths = env
    FEED["chan_a"] = [post("chan_a", 1, "FACTS:" + json.dumps([facts("Junior Data Scientist", "Acme")]))]
    run(config, storage, paths, FakeProvider())
    storage.db.execute("UPDATE vacancies SET published_at = '2026-08-01T00:00:00+00:00'")
    run(config, storage, paths, FakeProvider())
    assert storage.vacancies.feed() == []
    assert storage.vacancies.processed("auto_skipped")[0].skip_reason == "Expired"


def test_mail_flags_only_after_commit_for_evaluated_items(env):
    config, storage, paths = env
    FEED["chan_a"] = [post("chan_a", 1, "FACTS:" + json.dumps([facts("Junior Data Scientist", "Acme")]),
                           email={"uid": "42"})]
    run(config, storage, paths, FakeProvider(fail=True))
    assert FLAGGED == []
    run(config, storage, paths, FakeProvider())
    assert [m["email"]["uid"] for m in FLAGGED] == ["42"]


def teardown_module(module):
    for name in ("fake", "broken", "unconfigured"):
        _REGISTRY.pop(name, None)
