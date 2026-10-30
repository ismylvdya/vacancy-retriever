from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..collectors.base import RunCancelled
from ..config import AppConfig
from ..llm.base import LlmError, LlmProvider, LlmResponse, LlmUsage
from ..scoring.facts import VacancyFacts
from ..storage.items import StoredRawItem
from ..storage.llm_calls import LlmCallRecord, LlmCallRepository
from ..timeutil import to_iso

log = logging.getLogger(__name__)

RESULTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"input_id": {"type": "integer"}, "vacancies": {"type": "array", "items": {"type": "object"}}},
                "required": ["input_id", "vacancies"],
            },
        }
    },
    "required": ["results"],
}


# Structured facts collectors / e-mail parsers know for sure; passed to the LLM as reliable `hints`.
HINT_KEYS = ("required_languages", "company", "location", "city", "country", "work_format", "salary")


class LlmCaller:
    """One LLM call with retries, token accounting and cancellation."""

    def __init__(self, provider: LlmProvider, calls: LlmCallRepository, run_id: int | None, retries: int,
                 failures_dir: Path | None = None):
        self._provider = provider
        self._calls = calls
        self._run_id = run_id
        self._retries = retries
        self._failures_dir = failures_dir
        self.last_error: LlmError | None = None

    async def call(self, stage: str, system: str, user: str, model: str, n_items: int) -> LlmResponse:
        attempt = 0
        while True:
            started = time.monotonic()
            try:
                response = await self._provider.complete_json(system, user, model, RESULTS_SCHEMA)
            except asyncio.CancelledError:
                self._record(stage, model, n_items, started, None, ok=False, error="cancelled", unknown=True)
                raise
            except LlmError as exc:
                self._record(stage, model, n_items, started, None, ok=False, error=str(exc), unknown=exc.usage is None,
                             usage=exc.usage)
                self._keep_reply(stage, exc)
                self.last_error = exc
                # A reply that is not JSON comes back the same for the same input: one more try, then the caller
                # splits the batch (VacancyScorer.score) instead of repeating it.
                limit = min(self._retries, 1) if exc.kind == "parse" else self._retries
                if not exc.retryable or attempt >= limit:
                    raise
                attempt += 1
                if exc.kind != "parse":  # a bad reply is not a busy server: no reason to wait
                    await asyncio.sleep(5 * 2**attempt)
                continue
            self._record(stage, model, n_items, started, response, ok=True)
            return response

    def _keep_reply(self, stage: str, exc: LlmError) -> None:
        """Unparsable replies are kept in data/llm_failures/ (the newest 20) to see what the model got wrong."""
        if not self._failures_dir or not exc.raw_text:
            return
        try:
            self._failures_dir.mkdir(parents=True, exist_ok=True)
            name = f"{datetime.now():%Y%m%d-%H%M%S-%f}-{stage}.txt"
            (self._failures_dir / name).write_text(f"{exc}\n\n{exc.raw_text}", encoding="utf-8")
            for old in sorted(self._failures_dir.glob("*.txt"))[:-20]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    def _record(self, stage: str, model: str, n_items: int, started: float, response: LlmResponse | None,
                ok: bool, error: str | None = None, unknown: bool = False, usage: LlmUsage | None = None) -> None:
        usage = response.usage if response else usage
        self._calls.record(
            LlmCallRecord(
                run_id=self._run_id,
                stage=stage,
                provider=self._provider.name,
                model=(usage.model if usage and usage.model else model),
                n_items=n_items,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                cache_read_tokens=usage.cache_read_tokens if usage else None,
                cache_creation_tokens=usage.cache_creation_tokens if usage else None,
                ok=ok,
                duration_s=time.monotonic() - started,
                error=error,
                tokens_unknown=unknown,
                auth_source=response.auth_source if response else None,
                five_hour=usage.five_hour if usage else None, seven_day=usage.seven_day if usage else None,
                five_hour_resets_at=usage.five_hour_resets_at if usage else None,
            )
        )


def run_jobs[T](
    jobs: list[Callable[[], Awaitable[T]]],
    concurrency: int,
    cancel_event: threading.Event,
    on_done: Callable[[int], None] = lambda done: None,
) -> list[T | BaseException]:
    """Run async jobs with bounded concurrency; Cancel kills in-flight calls (and their CLI processes)."""

    async def main() -> list[T | BaseException]:
        semaphore = asyncio.Semaphore(concurrency)
        results: list[T | BaseException] = [RuntimeError("not run")] * len(jobs)
        done_count = 0

        async def guarded(index: int) -> None:
            nonlocal done_count
            async with semaphore:
                try:
                    results[index] = await jobs[index]()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - per-job failures are reported, not fatal
                    results[index] = exc
                done_count += 1
                on_done(done_count)

        tasks = [asyncio.create_task(guarded(i)) for i in range(len(jobs))]
        pending = set(tasks)
        while pending:
            if cancel_event.is_set():
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise RunCancelled()
            _, pending = await asyncio.wait(pending, timeout=0.3)
        return results

    return asyncio.run(main())


@dataclass
class ScoredItem:
    raw_id: int
    vacancies: list[VacancyFacts]


class VacancyScorer:
    """Builds batches, prompts the LLM, validates the extracted facts."""

    def __init__(self, config: AppConfig, caller: LlmCaller, source_names: dict[str, str]):
        self._config = config
        self._caller = caller
        self._source_names = source_names
        self._system = config.prompt("scoring") + "\n" + config.profile

    def batches(self, items: list[StoredRawItem]) -> list[tuple[str, list[StoredRawItem]]]:
        """Oldest posts first, and batches in that order too: if the Claude limit ends a run half-way, what stays
        unevaluated is the newest end, so the evaluated days form one unbroken stretch (P2, 28.10.2026)."""
        size = self._config.llm.batch_size
        items = sorted(items, key=_published_key)
        email = [i for i in items if i.meta.get("email")]
        other = [i for i in items if not i.meta.get("email")]
        result: list[tuple[str, list[StoredRawItem]]] = []
        for stage, group in (("scoring", other), ("scoring_email", email)):
            multi = [i for i in group if i.multi]
            single = [i for i in group if not i.multi]
            for chunk_source, chunk_size in ((single, size), (multi, max(1, size // 2))):
                for start in range(0, len(chunk_source), chunk_size):
                    result.append((stage, chunk_source[start : start + chunk_size]))
        return sorted(result, key=lambda entry: _published_key(entry[1][0]))

    def user_prompt(self, batch: list[StoredRawItem]) -> str:
        llm = self._config.llm
        payload = []
        for item in batch:
            limit = llm.max_input_chars_multi if item.multi else llm.max_input_chars
            text = item.full_text
            if item.title and item.title not in text[:300]:
                text = f"{item.title}\n{text}"
            entry: dict[str, Any] = {
                "input_id": item.id,
                "source": self._source_names.get(item.source_id, item.source_id),
                "published": (to_iso(item.published_at) or "")[:10],
                "text": text[:limit],
            }
            hints = {k: item.meta[k] for k in HINT_KEYS if item.meta.get(k)}
            if hints:
                entry["hints"] = hints
            payload.append(entry)
        return json.dumps(payload, ensure_ascii=False)

    async def score(self, stage: str, batch: list[StoredRawItem]) -> list[ScoredItem]:
        try:
            response = await self._caller.call(
                stage, self._system, self.user_prompt(batch), self._config.llm.stage_models.scoring, len(batch)
            )
        except LlmError as exc:
            if exc.kind != "parse" or len(batch) == 1:
                raise
            # One vacancy whose text makes the model write broken JSON must not hold back the rest (Run 27.10:
            # 20 items stayed unevaluated): halves are scored separately, down to the single culprit.
            half = len(batch) // 2
            log.info("Unparsable reply for %d items: scoring them in two halves", len(batch))
            first = await self._half(stage, batch[:half])
            second = await self._half(stage, batch[half:])
            if first is None and second is None:
                raise
            return (first or []) + (second or [])
        return parse_scoring_response(response.data, {i.id for i in batch})

    async def _half(self, stage: str, batch: list[StoredRawItem]) -> list[ScoredItem] | None:
        try:
            return await self.score(stage, batch)
        except LlmError as exc:
            if exc.kind != "parse":
                raise
            return None  # these stay pending_llm and go first in the next run


def _published_key(item: StoredRawItem) -> tuple[int, datetime]:
    """Sort key: by publication time, posts without a date last."""
    published = item.published_at
    return (0, published) if published else (1, datetime.max.replace(tzinfo=UTC))


def parse_scoring_response(data: dict[str, Any], expected_ids: set[int]) -> list[ScoredItem]:
    """Validated facts per input id. Ids missing in the reply are simply absent (stay pending)."""
    scored: list[ScoredItem] = []
    for entry in data.get("results") or []:
        try:
            raw_id = int(entry.get("input_id"))
        except (TypeError, ValueError):
            continue
        if raw_id not in expected_ids:
            continue
        vacancies = [VacancyFacts.model_validate(v) for v in entry.get("vacancies") or [] if isinstance(v, dict)]
        scored.append(ScoredItem(raw_id, vacancies))
    return scored


class DedupJudge:
    """Asks the LLM whether gray-zone pairs are the same vacancy."""

    BATCH = 20

    def __init__(self, config: AppConfig, caller: LlmCaller):
        self._config = config
        self._caller = caller
        self._system = config.prompt("dedup")

    def jobs(self, pairs: list[tuple[int, dict[str, Any], dict[str, Any]]]) -> list[Callable[[], Awaitable[set[int]]]]:
        chunks = [pairs[i : i + self.BATCH] for i in range(0, len(pairs), self.BATCH)]
        return [lambda chunk=chunk: self._judge(chunk) for chunk in chunks]

    async def _judge(self, chunk: list[tuple[int, dict[str, Any], dict[str, Any]]]) -> set[int]:
        user = json.dumps([{"pair_id": pid, "a": a, "b": b} for pid, a, b in chunk], ensure_ascii=False)
        response = await self._caller.call("dedup", self._system, user, self._config.llm.stage_models.dedup, len(chunk))
        same: set[int] = set()
        for entry in response.data.get("results") or []:
            if entry.get("same") is True:
                try:
                    same.add(int(entry.get("pair_id")))
                except (TypeError, ValueError):
                    continue
        return same
