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
DETAILS: dict[str, str] = {}


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

    def fetch_detail(self, item: RawItem) -> str | None:
        return DETAILS.get(item.detail_url or "")

    def detail_skip_reason(self, item: RawItem, text: str) -> str | None:
        return "Recruitment closed" if "Набор закрыт" in text else None


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
    DETAILS.clear()
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


class PoisonProvider(FakeProvider):
    """Writes broken JSON for any batch holding a POISON post (Run 27.10: 20 items stuck on such replies)."""

    async def complete_json(self, system: str, user: str, model: str, schema=None) -> LlmResponse:
        if "POISON" in user:
            self.calls += 1
            exc = LlmError("Invalid JSON in model reply: Expecting ',' delimiter", kind="parse")
            exc.usage, exc.raw_text = LlmUsage(100, 40, 0, 0, model), '{"results": [{"input_id": 1, oops'
            raise exc
        return await super().complete_json(system, user, model, schema)


def test_broken_json_for_one_post_does_not_hold_back_the_batch(env):
    config, storage, paths = env
    FEED["chan_a"] = [post("chan_a", i, "FACTS:" + json.dumps([facts(f"Junior Data Scientist {i}", f"Acme {i}")]))
                      for i in range(1, 4)] + [post("chan_a", 4, "POISON")]
    summary = run(config, storage, paths, PoisonProvider())
    assert summary.pending_llm == 1  # only the culprit waits
    assert len(storage.vacancies.feed()) == 3
    failed = [c for c in storage.llm_calls.by_run(summary.run_id) if not c["ok"]]
    assert failed and all(c["output_tokens"] == 40 for c in failed)  # tokens of failed calls are counted
    kept = list((paths.data / "llm_failures").glob("*.txt"))
    assert kept and "oops" in kept[0].read_text()


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


def _gray_pair(monkeypatch) -> None:
    # Similar company + title (a "gray" pair); the fuzzy repost check is switched off to test only the vacancy dedup.
    monkeypatch.setattr("vacancy_retriever.pipeline.dedup.RawDeduplicator._fuzzy", lambda self, item: None)
    FEED["chan_a"] = [post("chan_a", 1, "Команда ML в Белграде, офис у Славии. " * 8 + "FACTS:"
                           + json.dumps([facts("Data Scientist (junior)", "Acme Labs")]))]
    FEED["chan_b"] = [post("chan_b", 2, "Hiring for our modelling team, hybrid. " * 8 + "FACTS:"
                           + json.dumps([facts("Junior Data Scientist / ML", "Acme Labs d.o.o.")]))]


def test_gray_zone_duplicates_are_merged_by_llm_when_enabled(env, monkeypatch):
    config, storage, paths = env
    config.llm.dedup_with_llm = True
    _gray_pair(monkeypatch)
    run(config, storage, paths, FakeProvider(same_pairs=True))
    feed = storage.vacancies.feed()
    assert len(feed) == 1
    assert {s.source_id for s in feed[0].sources} == {"chan_a", "chan_b"}


def test_gray_zone_pairs_stay_apart_without_llm_dedup(env, monkeypatch):
    config, storage, paths = env
    config.llm.dedup_with_llm = False
    _gray_pair(monkeypatch)
    summary = run(config, storage, paths, FakeProvider(same_pairs=True))
    assert len(storage.vacancies.feed()) == 2
    assert [c for c in storage.llm_calls.by_run(summary.run_id) if c["stage"] == "dedup"] == []


def test_same_vacancy_on_a_site_and_in_a_channel_is_evaluated_once(env):
    config, storage, paths = env
    channel = post("chan_a", 1, "Мастер по ремонту квартир\nTRUSTONE ONE DOO BEOGRAD\n100 000 дин.")
    channel.multi = True
    card = RawItem("chan_b", "2", "https://jobs.example.com/2", "FACTS:" + json.dumps([facts(
        "Мастер по ремонту квартир", "TRUSTONE ONE DOO BEOGRAD", "manual_trades")]), NOW - timedelta(hours=1),
        title="Мастер по ремонту квартир", meta={"company": "TRUSTONE ONE DOO BEOGRAD"})
    FEED["chan_a"], FEED["chan_b"] = [channel], [card]
    provider = FakeProvider()
    run(config, storage, paths, provider)
    rows = {r["source_id"]: (r["status"], r["duplicate_of"]) for r in storage.db.query(
        "SELECT source_id, status, duplicate_of FROM raw_items")}
    assert rows["chan_b"] == ("scored", None), "the site card (richer text) is the original"
    assert rows["chan_a"][0] == "duplicate"
    [vacancy] = storage.vacancies.feed()
    assert {s.source_id for s in vacancy.sources} == {"chan_a", "chan_b"}


def test_gray_zone_pairs_of_skipped_vacancies_never_reach_llm(env):
    config, storage, paths = env
    FEED["chan_a"] = [post("chan_a", 1, "FACTS:" + json.dumps([facts("Senior Go Developer at Acme", "Acme", "other_dev")]))]
    FEED["chan_b"] = [post("chan_b", 2, "FACTS:" + json.dumps([facts("Senior Go Developer at Acme Labs", "Acme Labs", "other_dev")]))]
    summary = run(config, storage, paths, FakeProvider(same_pairs=True))
    assert [c for c in storage.llm_calls.by_run(summary.run_id) if c["stage"] == "dedup"] == []


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


def test_detail_page_can_drop_a_vacancy_before_llm(env):
    config, storage, paths = env
    config.__dict__["sources"].detail_domains.append("jobs.example.com")
    DETAILS["https://jobs.example.com/1"] = "Стажёр-аналитик. Набор закрыт"
    DETAILS["https://jobs.example.com/2"] = "Стажёр-аналитик. Идёт набор"
    for post_id in (1, 2):
        item = RawItem("chan_a", str(post_id), f"https://jobs.example.com/{post_id}", "FACTS:" + json.dumps(
            [facts("Junior Data Scientist", f"Acme {post_id}")]), NOW - timedelta(hours=1), title="Стажёр-аналитик",
            detail_url=f"https://jobs.example.com/{post_id}")
        FEED.setdefault("chan_a", []).append(item)
    provider = FakeProvider()
    run(config, storage, paths, provider)
    rows = {r["external_id"]: (r["status"], r["skip_reason"]) for r in storage.db.query(
        "SELECT external_id, status, skip_reason FROM raw_items WHERE source_id = 'chan_a'")}
    assert rows["1"] == ("prefiltered", "Recruitment closed")
    assert rows["2"][0] == "scored"


def teardown_module(module):
    for name in ("fake", "broken", "unconfigured"):
        _REGISTRY.pop(name, None)


def test_progress_lists_sources_done_failed_and_current(env):
    config, storage, paths = env
    FEED["chan_a"] = [post("chan_a", 1, "FACTS:" + json.dumps([facts("Junior Data Scientist", "Acme")]))]
    pipeline = make_pipeline(config, storage, paths, FakeProvider())
    pipeline.run(RunOptions(notify=False))
    state = pipeline.progress.snapshot()
    assert [s["id"] for s in state["sources"]] == ["chan_a", "chan_b", "broken", "mail"]
    assert set(state["done_ids"]) == {"chan_a", "chan_b", "mail"} and state["error_ids"] == ["broken"]
    assert state["current_id"] is None


def test_missed_period_after_a_break_is_remembered_until_collected(env):
    from vacancy_retriever.timeutil import to_iso

    config, storage, paths = env
    FEED["chan_a"] = [post("chan_a", 1, "FACTS:" + json.dumps([facts("Junior Data Scientist", "Acme")]))]
    first = run(config, storage, paths, FakeProvider())
    storage.db.execute("UPDATE runs SET started_at = ? WHERE id = ?", (to_iso(NOW - timedelta(days=10)), first.run_id))
    run(config, storage, paths, FakeProvider(), since=timedelta(days=1))  # back after 10 days, only 1 day asked
    missed = storage.runs.missed()
    assert missed["chan_a"] == (NOW - timedelta(days=10), NOW - timedelta(days=1))
    assert "mail" not in missed and "broken" not in missed  # e-mail is exempt, failed sources prove nothing
    run(config, storage, paths, FakeProvider(), backfill={"chan_a": missed["chan_a"][0]})
    assert FakeCollector.calls[-2].last_id is None or FakeCollector.calls[-1].last_id is None  # cursor ignored
    assert "chan_a" not in storage.runs.missed()
    storage.runs.clear_missed()
    assert storage.runs.missed() == {}


def test_run_start_removes_cv_files_of_earlier_applications(env):
    config, storage, paths = env
    folder = paths.data / "applications" / "5"
    folder.mkdir(parents=True)
    (folder / "Ismailov_CV_Acme_DA.pdf").write_bytes(b"%PDF")
    (folder / "note_mine.md").write_text("kept", encoding="utf-8")
    run(config, storage, paths, FakeProvider())
    assert not (folder / "Ismailov_CV_Acme_DA.pdf").exists() and (folder / "note_mine.md").exists()


def test_llm_gets_the_oldest_posts_first(env):
    config, storage, paths = env
    config.__dict__["llm"] = config.llm.model_copy(update={"batch_size": 2, "max_concurrency": 1})
    FEED["chan_a"] = [post("chan_a", i, "FACTS:" + json.dumps([facts(f"Junior Data Scientist {i}", f"Acme {i}")]))
                      for i in range(1, 6)]  # post i is i+1 hours old
    seen: list[list[int]] = []

    class Recording(FakeProvider):
        async def complete_json(self, system, user, model, schema=None):
            seen.append([int(e["text"].split("Scientist ")[1].split('"')[0]) for e in json.loads(user)])
            return await super().complete_json(system, user, model, schema)

    run(config, storage, paths, Recording())
    order = [n for batch in seen for n in batch]
    assert order == sorted(order, reverse=True), "the oldest (highest number) go first"
