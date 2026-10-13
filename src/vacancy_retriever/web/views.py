from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..config import AppConfig, SourceConfig
from ..llm.pricing import cost_usd
from ..storage import Storage
from ..storage.vacancies import VacancyRecord
from ..timeutil import format_local, from_iso

LANG_FLAGS = {
    "ru": "🇷🇺", "en": "🇬🇧", "sr": "🇷🇸", "de": "🇩🇪", "fr": "🇫🇷", "es": "🇪🇸", "it": "🇮🇹", "pl": "🇵🇱",
    "uk": "🇺🇦", "kk": "🇰🇿", "nl": "🇳🇱", "pt": "🇵🇹", "tr": "🇹🇷", "zh": "🇨🇳", "ja": "🇯🇵", "cs": "🇨🇿",
}
EMPLOYER_TYPES = {
    "serbian": "Serbian company",
    "ru_with_serbian_entity": "Russian-speaking, Serbian entity",
    "russian": "Russian company",
    "intl_russian_speaking": "International, Russian-speaking",
    "intl_english": "International, English-speaking",
    "private_person": "Private person",
}
WORK_FORMATS = {"office": "Office", "hybrid": "Hybrid", "remote": "Remote"}
PERMIT_LABELS = {"yes": "yes", "likely": "likely", "no": "no"}
STATUS_LABELS = {
    "applied": "Applied", "skipped": "Skipped", "not_now": "Not now", "auto_skipped": "Auto-skipped", "new": "New",
}
STAGE_ORDER = ["scoring", "scoring_email", "dedup"]
STAGE_LABELS = {"scoring": "Evaluation", "scoring_email": "Evaluation · e-mail (generic parser)", "dedup": "LLM dedup"}
ALERT_LABELS = {
    "http_403": "HTTP 403 — blocked?", "http_429": "HTTP 429 — rate limited", "captcha": "Captcha",
    "cloudflare": "Cloudflare challenge", "login_redirect": "Redirect to login", "not_json": "Unexpected response",
    "zero_items": "Source showed nothing (usually it does) — blocked or layout changed?", "mail_flags": "Could not label / move processed e-mails",
}


def lang_flag(code: str) -> str:
    return LANG_FLAGS.get(code, code.upper() if code and code != "other" else "🌐")


@dataclass
class SourceLink:
    source_id: str
    name: str
    url: str
    kind: str  # site | telegram | email


@dataclass
class Card:
    id: int
    title: str
    grade: str
    stars: int | None
    employer: str
    employer_type: str
    place: str
    permit: str | None
    permit_reason: str
    salary: str
    ad_language: str
    required_languages: list[str]
    summary: str
    english_flag: bool
    informal: bool
    published: str
    sites: list[SourceLink]
    channels: list[SourceLink]
    description: str
    user_status: str
    status_label: str
    skip_reason: str | None
    group: int | None
    category: str


class CardBuilder:
    def __init__(self, config: AppConfig, storage: Storage):
        self._sources: dict[str, SourceConfig] = {s.id: s for s in config.sources.sources}
        self._mailboxes = {m.id for m in config.mail.mailboxes}
        self._categories = config.rules.categories
        self._storage = storage

    def build(self, record: VacancyRecord) -> Card:
        facts = record.facts
        raw = self._storage.items.get(record.primary_raw_id)
        sites, channels = self._links(record)
        place = ", ".join(p for p in (
            WORK_FORMATS.get(facts.work_format, "") if facts else "",
            facts.city if facts and facts.city not in ("", "unknown") else "",
        ) if p)
        grade = record.seniority if record.seniority not in ("", "unknown") else ""
        category = self._categories.get(record.category)
        return Card(
            id=record.id,
            title=record.title,
            grade=grade.capitalize(),
            stars=record.stars,
            employer=record.employer,
            employer_type=EMPLOYER_TYPES.get(facts.employer_type, "") if facts else "",
            place=place,
            permit=PERMIT_LABELS.get(record.residence_permit or "") if facts else None,
            permit_reason=record.residence_reason or "",
            salary=facts.salary_text if facts else "",
            ad_language=lang_flag(facts.ad_language) if facts and facts.ad_language != "other" else "",
            required_languages=[lang_flag(code) for code in (facts.required_languages if facts else [])],
            summary=facts.summary_ru if facts else "",
            english_flag=record.english_flag,
            informal=record.informal_flag,
            published=format_local(record.published_at, "%d %b") if record.published_at else "",
            sites=sites,
            channels=channels,
            description=raw.full_text if raw else "",
            user_status=record.user_status,
            status_label=STATUS_LABELS.get(record.user_status, record.user_status),
            skip_reason=record.skip_reason,
            group=record.group_no,
            category=category.label if category else "",
        )

    def _links(self, record: VacancyRecord) -> tuple[list[SourceLink], list[SourceLink]]:
        sites: list[SourceLink] = []
        channels: list[SourceLink] = []
        seen: set[tuple[str, str]] = set()
        link = record.facts.link if record.facts else ""
        for source in record.sources:
            key = (source.source_id, source.url)
            if key in seen:
                continue
            seen.add(key)
            config = self._sources.get(source.source_id)
            name = config.name if config else source.source_id
            kind = "telegram" if config and config.type == "telegram" else (
                "email" if config and config.type == "email" else "site")
            url = link if kind == "email" and link else source.url
            entry = SourceLink(source.source_id, name, url, kind)
            (channels if kind == "telegram" else sites).append(entry)
        return sites, channels


def full_text(storage: Storage, vacancy_id: int) -> str:
    record = storage.vacancies.get(vacancy_id)
    if record is None:
        return ""
    raw = storage.items.get(record.primary_raw_id)
    if raw is None:
        return record.title
    parts = [raw.full_text.strip()]
    if raw.url:
        parts.append(raw.url)
    if record.facts and record.facts.link and record.facts.link not in parts:
        parts.append(record.facts.link)
    return "\n\n".join(parts)


def group_feed(cards: list[Card], config: AppConfig) -> list[dict[str, Any]]:
    by_group: dict[int, list[Card]] = defaultdict(list)
    for card in cards:
        by_group[card.group or 5].append(card)
    groups = []
    for number, info in sorted(config.rules.groups.items()):
        groups.append({"number": number, "title": info.title, "collapsed": info.collapsed, "note": info.note,
                       "cards": by_group.get(number, [])})
    return groups


# ---------------------------------------------------------------- health

@dataclass
class HealthRow:
    source_id: str
    name: str
    status: str
    collected: int
    passed: int
    to_feed: int
    duration: str
    error: str
    alert: str
    warn: bool


@dataclass
class HealthRun:
    id: int
    started: str
    duration_min: str
    status: str
    summary: dict[str, Any]
    rows: list[HealthRow] = field(default_factory=list)
    problems: int = 0


def health(config: AppConfig, storage: Storage, limit: int = 60) -> list[HealthRun]:
    names = {s.id: s.name for s in config.sources.sources}
    runs = [r for r in storage.runs.history(limit)]
    history: dict[str, list[int]] = defaultdict(list)
    result: list[HealthRun] = []
    for run in reversed(runs):
        started, finished = from_iso(run["started_at"]), from_iso(run["finished_at"])
        minutes = f"{(finished - started).total_seconds() / 60:.1f}" if started and finished else "…"
        health_run = HealthRun(run["id"], format_local(started), minutes, run["status"],
                               json.loads(run["summary_json"] or "{}"))
        for row in storage.runs.source_runs(run["id"]):
            source_id = row["source_id"]
            usual = history[source_id][-5:]
            alert = row["alert"] or ""
            seen = row["seen"] if row["seen"] is not None else row["collected"]
            if row["status"] in ("ok", "truncated") and seen == 0 and usual and max(usual) > 0:
                alert = alert or "zero_items"
            warn = row["status"] == "error" or bool(alert)
            if row["status"] in ("ok", "truncated", "error"):
                history[source_id].append(seen)
            health_run.rows.append(HealthRow(
                source_id, names.get(source_id, source_id), row["status"], row["collected"], row["passed"],
                row["to_feed"], f"{row['duration_s']:.0f}s", row["error"] or row["note"] or "",
                ALERT_LABELS.get(alert, alert), warn,
            ))
            health_run.problems += int(warn)
        result.append(health_run)
    return list(reversed(result))


def latest_source_states(runs: list[HealthRun]) -> list[HealthRow]:
    latest: dict[str, HealthRow] = {}
    for run in runs:
        if run.status == "cancelled":
            continue
        for row in run.rows:
            latest.setdefault(row.source_id, row)
    return sorted(latest.values(), key=lambda r: (not r.warn, r.status == "disabled", r.name.lower()))


# ---------------------------------------------------------------- tokens

def _cost(config: AppConfig, model: str, inp: int, out: int, cr: int, cw: int) -> float | None:
    price = config.llm.pricing.get(model)
    return cost_usd(price, inp, out, cr, cw) if price else None


def tokens(config: AppConfig, storage: Storage) -> dict[str, Any]:
    calls = storage.llm_calls.all()
    runs = {r["id"]: r for r in storage.runs.history(1000)}
    by_run: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for call in calls:
        by_run[call["run_id"]].append(call)
    comparison = config.llm.comparison_models
    run_views = []
    totals = {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "unknown": 0}
    total_costs = {m: 0.0 for m in comparison}
    for run_id in sorted(by_run, key=lambda r: r or 0, reverse=True):
        run_calls = by_run[run_id]
        stages: dict[str, dict[str, Any]] = {}
        for call in run_calls:
            stage = stages.setdefault(call["stage"], {
                "stage": call["stage"], "label": STAGE_LABELS.get(call["stage"], call["stage"]), "calls": 0,
                "failed": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "unknown": 0, "models": set()})
            stage["calls"] += 1
            stage["failed"] += 0 if call["ok"] else 1
            stage["unknown"] += call["tokens_unknown"]
            stage["input"] += call["input_tokens"] or 0
            stage["output"] += call["output_tokens"] or 0
            stage["cache_read"] += call["cache_read_tokens"] or 0
            stage["cache_write"] += call["cache_creation_tokens"] or 0
            if call["model"]:
                stage["models"].add(call["model"])
        run_total = sum(s["input"] + s["output"] + s["cache_read"] + s["cache_write"] for s in stages.values()) or 1
        ordered = sorted(stages.values(), key=lambda s: STAGE_ORDER.index(s["stage"]) if s["stage"] in STAGE_ORDER else 99)
        for stage in ordered:
            stage_total = stage["input"] + stage["output"] + stage["cache_read"] + stage["cache_write"]
            stage["total"] = stage_total
            stage["share"] = round(100 * stage_total / run_total, 1)
            stage["input_share"] = round(100 * (stage_total - stage["output"]) / (stage_total or 1))
            stage["output_share"] = 100 - stage["input_share"] if stage_total else 0
            stage["models"] = ", ".join(sorted(stage["models"]))
            stage["costs"] = {m: _cost(config, m, stage["input"], stage["output"], stage["cache_read"], stage["cache_write"])
                              for m in comparison}
            for key in ("calls", "input", "output", "cache_read", "cache_write", "unknown"):
                totals[key] += stage[key]
            for model, value in stage["costs"].items():
                total_costs[model] += value or 0.0
        run = runs.get(run_id) if run_id else None
        run_views.append({
            "run_id": run_id,
            "started": format_local(run["started_at"]) if run else "—",
            "status": run["status"] if run else "manual",
            "cancelled": bool(run and run["status"] == "cancelled") or any(c["cancelled"] for c in run_calls),
            "stages": ordered,
            "total": run_total if run_calls else 0,
            "costs": {m: sum((s["costs"][m] or 0.0) for s in ordered) for m in comparison},
        })
    return {"runs": run_views, "totals": totals, "total_costs": total_costs, "comparison": comparison,
            "provider": config.llm.provider, "auth_source": storage.llm_calls.last_auth_source()}
