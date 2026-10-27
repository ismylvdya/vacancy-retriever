from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..collectors.base import (
    CollectContext,
    Collector,
    CollectResult,
    RunCancelled,
    SourceCursor,
    SourceError,
    SourceNotConfigured,
)
from ..collectors.http import BlockedError, PoliteHttpClient, is_forbidden
from ..collectors.registry import collector_for
from ..config import AppConfig, SourceConfig
from ..icons import IconStore
from ..rates import RatesStore
from ..llm.base import LlmError, LlmProvider
from ..scoring.engine import ScoringEngine
from ..scoring.facts import VacancyFacts
from ..storage import Storage
from ..storage.items import StoredRawItem
from ..storage.runs import SourceRunRecord
from ..storage.vacancies import NewVacancy
from ..timeutil import format_local, local, to_iso, utcnow
from .dedup import RawDeduplicator, compare_vacancies, dedup_key
from .llm_stage import DedupJudge, LlmCaller, ScoredItem, VacancyScorer, run_jobs
from .prefilter import Prefilter
from .progress import ProgressTracker, SourceStats

log = logging.getLogger(__name__)

TIME_CURSOR_MARGIN = timedelta(hours=1)
DEDUP_WINDOW_DAYS = 30


@dataclass
class RunOptions:
    source_ids: list[str] | None = None
    limit: int | None = None
    since: timedelta | None = None
    trigger: str = "ui"
    notify: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_ids": self.source_ids,
            "limit": self.limit,
            "since_hours": self.since.total_seconds() / 3600 if self.since else None,
            "trigger": self.trigger,
        }


@dataclass
class RunSummary:
    run_id: int
    status: str
    new_by_group: dict[int, int] = field(default_factory=dict)
    errors: int = 0
    error_sources: list[str] = field(default_factory=list)
    pending_llm: int = 0
    llm_error: str | None = None
    duration_s: float = 0.0
    started_at: str | None = None

    @property
    def new_total(self) -> int:
        return sum(self.new_by_group.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "new_by_group": {str(k): v for k, v in sorted(self.new_by_group.items())},
            "new_total": self.new_total,
            "errors": self.errors,
            "error_sources": self.error_sources,
            "pending_llm": self.pending_llm,
            "llm_error": self.llm_error,
            "duration_s": round(self.duration_s, 1),
        }


class Pipeline:
    """One run: collect → raw dedup → prefilter → details → LLM facts → vacancy dedup → commit."""

    def __init__(
        self,
        config: AppConfig,
        storage: Storage,
        provider_factory: Callable[[], LlmProvider],
        progress: ProgressTracker,
        cancel_event: threading.Event,
        http_factory: Callable[[], PoliteHttpClient] = PoliteHttpClient,
        notifier: Callable[[str], None] | None = None,
        now: Callable[[], datetime] = utcnow,
    ):
        self.config = config
        self.storage = storage
        self.provider_factory = provider_factory
        self.progress = progress
        self.cancel_event = cancel_event
        self.http_factory = http_factory
        self.notifier = notifier
        self.now = now
        self.engine = ScoringEngine(config.rules)
        self.prefilter = Prefilter(config.rules)
        self.icons = IconStore(config.paths)
        self.stats = SourceStats(config.paths.source_stats)
        self._collectors: dict[str, Collector] = {}
        self._source_status: dict[str, str] = {}
        self._llm_error: str | None = None
        self._scored_previous: set[int] = set()

    # ------------------------------------------------------------------ entry point
    def run(self, options: RunOptions) -> RunSummary:
        started = time.monotonic()
        run_start = self.now()
        run_id = self.storage.runs.start(options.as_dict())
        sources = self._selected_sources(options)
        self.progress.start(run_id, to_iso(run_start) or "", os.getpid(),
                            {s.id: self.stats.weight(s) for s in sources if s.enabled},
                            [(s.id, s.name) for s in sources if s.enabled])
        http = self.http_factory()
        self._rates = RatesStore(self.config.paths).fetch_today(http, local(run_start).date())
        try:
            self._collect_all(run_id, sources, options, run_start, http)
            self._check_cancel()
            self._prefilter(run_id)
            self._fetch_details(run_id)
            self._language_filter(run_id)
            self._score(run_id)
            self._dedup(run_id)
            self._check_cancel()
            summary = self._commit(run_id, started, run_start)
        except RunCancelled:
            self.storage.runs.rollback(run_id, "cancelled")
            self.progress.finish("cancelled", "Run cancelled; nothing was saved")
            return RunSummary(run_id, "cancelled", duration_s=time.monotonic() - started)
        except Exception as exc:
            log.exception("Run %s failed", run_id)
            self.storage.runs.rollback(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
            self.progress.finish("failed", f"Run failed: {exc}")
            if options.notify and self.notifier:
                self.notifier(f"Run failed: {exc}")
            raise
        finally:
            http.close()
            self.stats.save()
        self._after_commit(summary)
        self.progress.finish("done", self._summary_line(summary))
        if options.notify and self.notifier:
            self.notifier(self._notification(summary))
        return summary

    def _selected_sources(self, options: RunOptions) -> list[SourceConfig]:
        sources = self.config.sources.sources
        if options.source_ids:
            wanted = set(options.source_ids)
            unknown = wanted - {s.id for s in sources}
            if unknown:
                raise ValueError(f"Unknown source ids: {', '.join(sorted(unknown))}")
            sources = [s for s in sources if s.id in wanted]
        return sources

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise RunCancelled()

    # ------------------------------------------------------------------ 1. collect
    def _collect_all(self, run_id: int, sources: list[SourceConfig], options: RunOptions,
                     run_start: datetime, http: PoliteHttpClient) -> None:
        raw_dedup = RawDeduplicator(self.storage.items, run_id=run_id)
        for source in sources:
            self._check_cancel()
            if not source.enabled:
                self.storage.runs.record_source(run_id, SourceRunRecord(source.id, "disabled",
                                                                        note=source.disabled_reason))
                continue
            self.progress.source_started(source.name, source.id)
            record = self._collect_source(run_id, source, options, run_start, http, raw_dedup)
            self.storage.runs.record_source(run_id, record)
            self._source_status[source.id] = record.status
            if record.status in ("ok", "truncated", "error"):
                self.stats.observe(source.id, record.duration_s)
            self.progress.source_done(source.id, source.name, failed=record.status == "error")

    def _collect_source(self, run_id: int, source: SourceConfig, options: RunOptions, run_start: datetime,
                        http: PoliteHttpClient, raw_dedup: RawDeduplicator) -> SourceRunRecord:
        started = time.monotonic()
        rules = self.config.rules
        cursor = self.storage.runs.cursor(source.id)
        first_run = not self.storage.runs.has_cursor(source.id)
        window = options.since or timedelta(days=rules.first_run_days if first_run else rules.max_age_days)
        ctx = CollectContext(
            cursor=cursor,
            since=run_start - window,
            max_pages=source.max_pages,
            is_known=lambda external_id: self.storage.items.is_known(source.id, external_id),
            cancel_event=self.cancel_event,
            limit=options.limit,
            on_page=lambda page: self.progress.source_page(source.id, source.name, page, source.max_pages),
        )
        try:
            collector = collector_for(source, http)
            self._collectors[source.id] = collector
            result = collector.collect(ctx)
        except RunCancelled:
            raise
        except SourceNotConfigured as exc:
            return SourceRunRecord(source.id, "not_configured", note=str(exc),
                                   duration_s=time.monotonic() - started)
        except BlockedError as exc:
            return SourceRunRecord(source.id, "error", error=str(exc), alert=exc.alert,
                                   duration_s=time.monotonic() - started)
        except SourceError as exc:
            return SourceRunRecord(source.id, "error", error=str(exc), alert=exc.alert,
                                   duration_s=time.monotonic() - started)
        except Exception as exc:  # noqa: BLE001 - one broken source must not stop the others
            log.exception("Source %s failed", source.id)
            return SourceRunRecord(source.id, "error", error=f"{type(exc).__name__}: {exc}",
                                   duration_s=time.monotonic() - started)
        stored = self._store_items(run_id, source, result, raw_dedup)
        new_cursor = result.new_cursor or SourceCursor(last_published_at=run_start - TIME_CURSOR_MARGIN)
        self.storage.runs.stage_cursor(run_id, source.id, new_cursor)
        self.icons.refresh(source, result.icon_url or collector.icon_url(), http)
        self._refresh_sender_icons(collector, http)
        seen = result.seen if result.seen is not None else len(result.items)
        return SourceRunRecord(source.id, result.status, collected=stored, seen=seen, note=result.note,
                               duration_s=time.monotonic() - started)

    def _refresh_sender_icons(self, collector: Collector, http: PoliteHttpClient) -> None:
        """E-mail senders (hh.ru, VK…): favicon of their site, once a month; an icon is never worth an error."""
        from ..collectors.sites._common import favicon_from_html

        for icon_id, site in getattr(collector, "sender_icons", lambda: [])():
            stand_in = SourceConfig(id=icon_id, type="email", name=icon_id)
            if self.icons.user_icon(stand_in) or not self.icons.needs_refresh(icon_id) or is_forbidden(site):
                continue
            try:
                url = favicon_from_html(http.get_text(site), site)
            except Exception as exc:  # noqa: BLE001
                log.info("Sender icon page %s failed: %s", site, exc)
                continue
            self.icons.refresh(stand_in, url, http)

    def _store_items(self, run_id: int, source: SourceConfig, result: CollectResult,
                     raw_dedup: RawDeduplicator) -> int:
        stored = 0
        for item in result.items:
            item.source_id = source.id
            if self.storage.items.is_known(source.id, item.external_id):
                continue
            original = raw_dedup.find_original(item)
            swap_with = None
            if original is None:
                match = raw_dedup.find_same_title(item)
                if match and match.swap:
                    swap_with = match.raw_id
                elif match:
                    original = match.raw_id
            raw_id = self.storage.items.insert(
                run_id, item, "duplicate" if original else "pending_llm", duplicate_of=original
            )
            if raw_id is None:
                continue
            stored += 1
            if swap_with is not None:
                self.storage.items.make_duplicate(swap_with, raw_id)
            if original:
                for vacancy_id in self.storage.vacancies.ids_for_raw(original):
                    self.storage.vacancies.attach_source(run_id, vacancy_id, raw_id, source.id, item.url)
            else:
                raw_dedup.remember(raw_id, item)
        return stored

    # ------------------------------------------------------------------ 2. prefilter
    def _prefilter(self, run_id: int) -> None:
        self.progress.phase("prefilter", "Filtering")
        now = self.now()
        for item in self.storage.items.by_run(run_id, "pending_llm"):
            result = self.prefilter.check(item, now)
            if result.action == "keep":
                continue
            if result.action == "hide":
                self.storage.items.set_status(item.id, "hidden", result.reason)
                continue
            self._skip_before_llm(run_id, item, result.reason)

    def _skip_before_llm(self, run_id: int, item: StoredRawItem, reason: str | None) -> None:
        self.storage.items.set_status(item.id, "prefiltered", reason)
        vacancy_id = self.storage.vacancies.insert(run_id, NewVacancy(
            primary_raw_id=item.id, origin="prefilter",
            title=item.title or item.text.split("\n", 1)[0][:140],
            employer=item.meta.get("company", ""), category="", seniority=item.meta.get("grade", "unknown"),
            decision="skip", skip_reason=reason, published_at=item.published_at,
        ))
        self._attach_with_duplicates(run_id, vacancy_id, item)

    def _attach_with_duplicates(self, run_id: int, vacancy_id: int, item: StoredRawItem) -> None:
        self.storage.vacancies.attach_source(run_id, vacancy_id, item.id, item.source_id, item.url)
        for duplicate in self.storage.items.duplicates_of(item.id):
            self.storage.vacancies.attach_source(run_id, vacancy_id, duplicate.id, duplicate.source_id, duplicate.url)

    # ------------------------------------------------------------------ 3. details
    def _fetch_details(self, run_id: int) -> None:
        allowed = {d.lower() for d in self.config.sources.detail_domains}
        items = [i for i in self.storage.items.by_run(run_id, "pending_llm") if i.meta.get("detail_url")]
        for index, item in enumerate(items):
            self._check_cancel()
            self.progress.phase("details", f"Fetching descriptions {index + 1}/{len(items)}", index / max(len(items), 1))
            url = item.meta["detail_url"]
            collector = self._collectors.get(item.source_id)
            if not collector or not collector.can_fetch_detail(url, allowed):
                continue
            try:
                text = collector.fetch_detail(_as_raw(item))
            except RunCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - listing text is still usable
                log.info("Detail fetch failed for %s: %s", url, exc)
                continue
            if text:
                self.storage.items.set_detail(item.id, text)
                reason = collector.detail_skip_reason(_as_raw(item), text)
                if reason:
                    self._skip_before_llm(run_id, item, reason)
        for source_id, collector in self._collectors.items():
            alert = collector.detail_alert()
            if alert:
                self.storage.db.execute("UPDATE source_runs SET note = ? WHERE run_id = ? AND source_id = ?",
                                        (alert[:500], run_id, source_id))

    def _language_filter(self, run_id: int) -> None:
        """Sources marked `serbian_needs_russian`: Serbian ads without Russian never reach the LLM."""
        marked = {s.id for s in self.config.sources.sources if s.params.get("serbian_needs_russian")}
        if not marked:
            return
        for item in self.storage.items.by_run(run_id, "pending_llm"):
            if item.source_id not in marked:
                continue
            result = self.prefilter.check_language(item, f"{item.text}\n{item.detail_text or ''}")
            if result.action == "skip":
                self._skip_before_llm(run_id, item, result.reason)

    # ------------------------------------------------------------------ 4. LLM facts
    def _score(self, run_id: int) -> None:
        pending = self.storage.items.pending_from_previous_runs() + self.storage.items.by_run(run_id, "pending_llm")
        if not pending:
            return
        self.progress.phase("scoring", f"Evaluating {_plural(len(pending), 'item')} with LLM")
        try:
            provider = self.provider_factory()
        except LlmError as exc:
            self._llm_error = str(exc)
            return
        caller = LlmCaller(provider, self.storage.llm_calls, run_id, self.config.llm.retries)
        names = {s.id: s.name for s in self.config.sources.sources}
        scorer = VacancyScorer(self.config, caller, names)
        batches = scorer.batches(pending)
        jobs = [lambda stage=stage, batch=batch: scorer.score(stage, batch) for stage, batch in batches]
        results = run_jobs(
            jobs, self.config.llm.max_concurrency, self.cancel_event,
            on_done=lambda done: self.progress.phase(
                "scoring", f"Evaluated {done} of {_plural(len(batches), 'batch')}", done / len(batches)),
        )
        by_id = {item.id: item for item in pending}
        now = self.now()
        for result in results:
            if isinstance(result, BaseException):
                self._llm_error = str(result)
                continue
            for scored in result:
                self._store_scored(run_id, by_id[scored.raw_id], scored, now)

    def _store_scored(self, run_id: int, item: StoredRawItem, scored: ScoredItem, now: datetime) -> None:
        for facts in scored.vacancies:
            vacancy = self._vacancy_from_facts(item, facts, now)
            if vacancy is None:
                continue
            vacancy_id = self.storage.vacancies.insert(run_id, vacancy)
            self._attach_with_duplicates(run_id, vacancy_id, item)
        if item.staged:
            self.storage.items.set_status(item.id, "scored")
        else:
            self.storage.runs.stage_raw_status(run_id, item.id, "scored")
            self._scored_previous.add(item.id)

    def _vacancy_from_facts(self, item: StoredRawItem, facts: VacancyFacts, now: datetime) -> NewVacancy | None:
        if not facts.employer_name or facts.employer_name == "unknown":
            facts.employer_name = item.meta.get("company") or facts.employer_name
        verdict = self.engine.evaluate(facts, item.published_at, now, getattr(self, "_rates", None))
        if verdict.decision == "hidden":
            return None
        title = item.title if (not item.multi and item.title) else facts.display_title
        return NewVacancy(
            primary_raw_id=item.id, origin="llm", title=title,
            employer=facts.employer_name if facts.employer_name != "unknown" else "",
            category=facts.role_category, seniority=facts.seniority, decision=verdict.decision,
            skip_reason=verdict.skip_reason, published_at=item.published_at, facts=facts, verdict=verdict,
            dedup_key=dedup_key(facts.employer_name, facts.display_title),
        )

    # ------------------------------------------------------------------ 5. vacancy dedup
    def _dedup(self, run_id: int) -> None:
        new = self.storage.vacancies.staged_for_run(run_id)
        if not new:
            return
        self.progress.phase("dedup", "Removing duplicates")
        candidates = self.storage.vacancies.dedup_candidates(DEDUP_WINDOW_DAYS)
        merged: dict[int, int] = {}
        gray: list[tuple[int, int]] = []
        for vacancy in new:
            for candidate in candidates:
                if candidate["id"] >= vacancy.id or candidate["primary_raw_id"] == vacancy.primary_raw_id:
                    continue
                if candidate["id"] in merged:
                    continue
                verdict = compare_vacancies(vacancy.employer, vacancy.title,
                                            candidate["employer"] or "", candidate["title"])
                if verdict == "exact":
                    merged[vacancy.id] = candidate["id"]
                    break
                # The LLM judges gray pairs only when both sides are visible vacancies: merging two
                # auto-skipped posts (hundreds of similar RVC titles) changes nothing the user sees.
                if verdict == "gray" and vacancy.decision == "show" and candidate.get("decision") == "show":
                    gray.append((vacancy.id, candidate["id"]))
        gray = [(a, b) for a, b in gray if a not in merged]
        if gray and self.config.llm.dedup_with_llm:
            merged.update(self._judge_gray(run_id, gray, merged))
        for vacancy_id in sorted(merged):
            target = merged[vacancy_id]
            while target in merged:
                target = merged[target]
            self.storage.vacancies.move_sources_and_delete(run_id, vacancy_id, target)

    def _judge_gray(self, run_id: int, gray: list[tuple[int, int]], merged: dict[int, int]) -> dict[int, int]:
        try:
            provider = self.provider_factory()
        except LlmError as exc:
            self._llm_error = str(exc)
            return {}
        judge = DedupJudge(self.config, LlmCaller(provider, self.storage.llm_calls, run_id, self.config.llm.retries))
        describe = {}
        for vacancy_id in {i for pair in gray for i in pair}:
            record = self.storage.vacancies.get(vacancy_id)
            if record:
                describe[vacancy_id] = {
                    "title": record.title, "employer": record.employer,
                    "city": record.facts.city if record.facts else "",
                    "summary": (record.facts.summary_ru if record.facts else "")[:200],
                }
        pairs = [(index, describe[a], describe[b]) for index, (a, b) in enumerate(gray) if a in describe and b in describe]
        results = run_jobs(judge.jobs(pairs), self.config.llm.max_concurrency, self.cancel_event)
        same: set[int] = set()
        for result in results:
            if isinstance(result, BaseException):
                self._llm_error = str(result)
            else:
                same |= result
        decided: dict[int, int] = {}
        for index in sorted(same):
            new_id, existing_id = gray[index]
            if new_id not in decided and new_id not in merged:
                decided[new_id] = existing_id
        return decided

    # ------------------------------------------------------------------ 6. commit
    def _commit(self, run_id: int, started: float, run_start: datetime) -> RunSummary:
        self.progress.phase("committing", "Saving")
        self._update_source_counts(run_id)
        records = self.storage.runs.source_runs(run_id)
        error_sources = [r["source_id"] for r in records if r["status"] == "error"]
        summary = RunSummary(
            run_id=run_id, status="committed",
            new_by_group=self.storage.vacancies.new_in_feed_for_run(run_id),
            errors=len(error_sources), error_sources=error_sources,
            llm_error=self._llm_error, duration_s=time.monotonic() - started, started_at=to_iso(run_start),
        )
        self._check_cancel()
        self.storage.runs.commit(run_id, summary.as_dict(), self.config.rules.max_age_days, self.now())
        summary.pending_llm = self.storage.items.count_pending()
        if summary.pending_llm:
            self.storage.runs.db.execute(
                "UPDATE runs SET summary_json = json_set(summary_json, '$.pending_llm', ?) WHERE id = ?",
                (summary.pending_llm, run_id),
            )
        return summary

    def _update_source_counts(self, run_id: int) -> None:
        rows = self.storage.db.query(
            "SELECT r.source_id, SUM(CASE WHEN r.status IN ('pending_llm', 'scored') THEN 1 ELSE 0 END) AS passed,"
            " (SELECT COUNT(DISTINCT vs.vacancy_id) FROM vacancy_sources vs JOIN vacancies v ON v.id = vs.vacancy_id"
            "  WHERE vs.source_id = r.source_id AND vs.run_id = ? AND v.user_status = 'new') AS to_feed"
            " FROM raw_items r WHERE r.run_id = ? GROUP BY r.source_id",
            (run_id, run_id),
        )
        for row in rows:
            self.storage.runs.update_source_counts(run_id, row["source_id"], row["passed"] or 0, row["to_feed"] or 0)

    def _after_commit(self, summary: RunSummary) -> None:
        """Mail flags: only for e-mails fully evaluated in this run, only after the commit."""
        processed: dict[str, list[dict[str, Any]]] = defaultdict(list)
        ids = sorted(self._scored_previous)
        marks = ",".join("?" * len(ids)) or "NULL"
        rows = self.storage.db.query(
            "SELECT source_id, meta_json FROM raw_items WHERE meta_json LIKE '%\"email\"%'"
            f" AND status != 'pending_llm' AND (run_id = ? OR id IN ({marks}))",
            (summary.run_id, *ids),
        )
        pending_uids = self._pending_email_uids()
        for row in rows:
            meta = json.loads(row["meta_json"] or "{}")
            email_meta = meta.get("email") or {}
            if email_meta and (row["source_id"], str(email_meta.get("uid"))) not in pending_uids:
                processed[row["source_id"]].append(meta)
        for source_id, metas in processed.items():
            collector = self._collectors.get(source_id)
            if collector is None:
                continue
            errors = collector.after_commit(metas)
            if errors:
                log.warning("after_commit %s: %s", source_id, errors)
                self.storage.db.execute(
                    "UPDATE source_runs SET alert = 'mail_flags', error = ? WHERE run_id = ? AND source_id = ?",
                    ("; ".join(errors)[:500], summary.run_id, source_id),
                )

    def _pending_email_uids(self) -> set[tuple[str, str]]:
        """A letter split into several items is flagged only when none of them waits for the LLM."""
        rows = self.storage.db.query(
            "SELECT source_id, meta_json FROM raw_items WHERE status = 'pending_llm' AND meta_json LIKE '%\"email\"%'"
        )
        result: set[tuple[str, str]] = set()
        for row in rows:
            email_meta = json.loads(row["meta_json"] or "{}").get("email") or {}
            result.add((row["source_id"], str(email_meta.get("uid"))))
        return result

    # ------------------------------------------------------------------ texts
    @staticmethod
    def _summary_line(summary: RunSummary) -> str:
        groups = " / ".join(str(summary.new_by_group.get(g, 0)) for g in range(1, 6))
        line = f"Run {format_local(summary.started_at)} · new {summary.new_total} ({groups})"
        if summary.errors:
            line += f" · errors {summary.errors}"
        return line

    @staticmethod
    def _notification(summary: RunSummary) -> str:
        text = f"Run finished: {summary.new_total} new"
        if summary.errors:
            text += f" · ⚠ {summary.errors} sources with errors"
        if summary.pending_llm:
            text += f" · {summary.pending_llm} waiting for LLM"
        return text


def _plural(count: int, word: str) -> str:
    suffix = "es" if word.endswith("ch") else "s"
    return f"{count} {word}{'' if count == 1 else suffix}"


def _as_raw(item: StoredRawItem):
    from ..collectors.base import RawItem

    return RawItem(
        source_id=item.source_id, external_id=item.external_id, url=item.url, text=item.text,
        published_at=item.published_at, title=item.title, multi=item.multi,
        detail_url=item.meta.get("detail_url"), meta=item.meta,
    )
