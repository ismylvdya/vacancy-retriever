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

LANGUAGES = {
    "ru": "Russian", "en": "English", "sr": "Serbian", "de": "German", "fr": "French", "es": "Spanish",
    "it": "Italian", "pl": "Polish", "uk": "Ukrainian", "kk": "Kazakh", "ky": "Kyrgyz", "nl": "Dutch",
    "pt": "Portuguese", "tr": "Turkish", "zh": "Chinese", "ja": "Japanese", "cs": "Czech",
}
WORK_FORMATS = {"office": "Office", "hybrid": "Hybrid", "remote": "Remote"}
PERMIT_LABELS = {"yes": "Yes", "likely": "Likely", "no": "No"}
STATUS_LABELS = {
    "applied": "Applied", "skipped": "Skipped", "not_now": "Not now", "auto_skipped": "Auto-skipped", "new": "New",
}
PERMIT_GROUPS = {1, 2}
STAGE_ORDER = ["scoring", "scoring_email", "dedup"]
STAGE_LABELS = {"scoring": "Evaluation", "scoring_email": "Evaluation of e-mails (generic parser)", "dedup": "Duplicate check"}
ALERT_LABELS = {
    "http_403": "Blocked (HTTP 403)", "http_429": "Rate limited (HTTP 429)", "captcha": "Captcha",
    "cloudflare": "Cloudflare challenge", "login_redirect": "Redirect to login", "not_json": "Unexpected response",
    "zero_items": "Showed nothing — blocked or layout changed?", "mail_flags": "Could not label / move processed e-mails",
}


def _known(value: str | None) -> str:
    return "" if not value or value.strip().lower() in {"unknown", "n/a", "none"} else value.strip()


def _salary(value: str | None) -> str:
    """'По договорённости' and the like are not a salary — show nothing."""
    value = _known(value)
    return value if any(ch.isdigit() for ch in value) else ""


@dataclass
class SourceLink:
    source_id: str
    name: str
    url: str
    kind: str  # site | telegram | email


@dataclass
class Card:
    """One vacancy for the UI. Every field is shown in its own place — never concatenated."""

    id: int
    title: str
    employer: str
    fit: int | None
    permit: str
    permit_level: str
    permit_reason: str
    work_format: str
    city: str
    country: str
    salary: str
    published: str
    is_new: bool
    summary: str
    flags: list[str]
    languages_note: str
    sources: list[SourceLink]
    description: str
    user_status: str
    status_label: str
    skip_reason: str | None
    group: int | None

    @property
    def low_fit(self) -> bool:
        return self.fit is not None and self.fit <= 2


class CardBuilder:
    def __init__(self, config: AppConfig, storage: Storage):
        self._sources: dict[str, SourceConfig] = {s.id: s for s in config.sources.sources}
        self._storage = storage
        latest = storage.runs.last_finished()
        self._latest_run = latest["id"] if latest and latest["status"] == "committed" else None

    def build(self, record: VacancyRecord) -> Card:
        facts = record.facts
        raw = self._storage.items.get(record.primary_raw_id)
        flags = []
        if record.english_flag:
            flags.append("English team")
        if record.informal_flag:
            flags.append("Informal")
        return Card(
            id=record.id,
            title=record.title,
            employer=_known(record.employer),
            fit=record.stars,
            permit=PERMIT_LABELS.get(record.residence_permit or "", ""),
            permit_level=record.residence_permit or "",
            permit_reason=_known(record.residence_reason),
            work_format=WORK_FORMATS.get(facts.work_format, "") if facts else "",
            city=_known(facts.city) if facts else "",
            country=_known(facts.country) if facts else "",
            salary=_salary(facts.salary_text) if facts else "",
            published=format_local(record.published_at, "%-d %b") if record.published_at else "",
            is_new=record.run_id == self._latest_run and record.user_status == "new",
            summary=_known(facts.summary_ru) if facts else "",
            flags=flags,
            languages_note=self._languages_note(record),
            sources=self._links(record),
            description=raw.full_text if raw else "",
            user_status=record.user_status,
            status_label=STATUS_LABELS.get(record.user_status, record.user_status),
            skip_reason=record.skip_reason,
            group=record.group_no,
        )

    @staticmethod
    def _languages_note(record: VacancyRecord) -> str:
        """Only when languages are a risk: anything required besides Russian."""
        facts = record.facts
        if facts is None:
            return ""
        other = [LANGUAGES.get(code, code.upper()) for code in facts.required_languages if code != "ru"]
        return ", ".join(other)

    def _links(self, record: VacancyRecord) -> list[SourceLink]:
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
            (channels if kind == "telegram" else sites).append(SourceLink(source.source_id, name, url, kind))
        return sites + channels


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
    if cards and all(card.is_new for card in cards):
        for card in cards:
            card.is_new = False
    by_group: dict[int, list[Card]] = defaultdict(list)
    for card in cards:
        by_group[card.group or 5].append(card)
    groups = []
    for number, info in sorted(config.rules.groups.items()):
        groups.append({"number": number, "title": info.title, "collapsed": info.collapsed, "note": info.note,
                       "permit_column": number in PERMIT_GROUPS, "cards": by_group.get(number, [])})
    return groups


# ---------------------------------------------------------------- health

@dataclass
class HealthRow:
    source_id: str
    name: str
    status: str
    collected: int
    seen: int | None
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
                source_id, names.get(source_id, source_id), row["status"], row["collected"], row["seen"], row["passed"],
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

def model_label(model: str) -> str:
    """claude-haiku-5-5 → Haiku 5.5, gpt-5.4-nano → GPT-5.4 nano."""
    if model.startswith("claude-"):
        family, *version = model.removeprefix("claude-").split("-")
        return f"{family.capitalize()} {'.'.join(version)}".strip()
    if model.startswith("gpt-"):
        return "GPT-" + model.removeprefix("gpt-").replace("-", " ", 1)
    return model


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
            stage["models"] = ", ".join(model_label(m) for m in sorted(stage["models"]))
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
            "labels": {m: model_label(m) for m in comparison},
            "provider": config.llm.provider, "auth_source": storage.llm_calls.last_auth_source()}
