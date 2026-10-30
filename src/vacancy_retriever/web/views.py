from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..apply.models import Application
from ..config import AppConfig, SourceConfig
from ..storage import Storage
from ..storage.vacancies import VacancyRecord
from ..rates import RatesStore
from ..scoring.salary import format_rub, monthly_rub, parse_salary
from ..timeutil import format_local, from_iso, local

LANGUAGES = {
    "ru": "Russian", "en": "English", "sr": "Serbian", "de": "German", "fr": "French", "es": "Spanish",
    "it": "Italian", "pl": "Polish", "uk": "Ukrainian", "kk": "Kazakh", "ky": "Kyrgyz", "nl": "Dutch",
    "pt": "Portuguese", "tr": "Turkish", "zh": "Chinese", "ja": "Japanese", "cs": "Czech",
}
WORK_FORMATS = {"office": "Office", "hybrid": "Hybrid", "remote": "Remote"}
FLAGS = {
    "ru": "🇷🇺", "en": "🇬🇧", "sr": "🇷🇸", "de": "🇩🇪", "fr": "🇫🇷", "es": "🇪🇸", "it": "🇮🇹", "pl": "🇵🇱", "uk": "🇺🇦",
    "kk": "🇰🇿", "ky": "🇰🇬", "nl": "🇳🇱", "pt": "🇵🇹", "tr": "🇹🇷", "zh": "🇨🇳", "ja": "🇯🇵", "cs": "🇨🇿",
}
GRADES = {"intern": "Intern", "junior": "Junior", "middle": "Middle", "senior": "Senior", "lead": "Lead"}
EMPLOYER_TYPES = {
    "serbian": "Serbian company", "ru_with_serbian_entity": "Russian company with a Serbian d.o.o.",
    "russian": "Russian company", "intl_russian_speaking": "International, Russian-speaking",
    "intl_english": "International, English-speaking", "private_person": "Private person",
}
PERMIT_LABELS = {"yes": "Yes", "likely": "Likely", "no": "No"}
STATUS_LABELS = {
    "applied": "Applied", "skipped": "Skipped", "not_now": "Not now", "auto_skipped": "Auto-skipped", "new": "New",
}
PERMIT_GROUPS = {1, 2}
ALERT_LABELS = {
    "http_403": "Blocked (HTTP 403)", "http_429": "Rate limited (HTTP 429)", "captcha": "Captcha",
    "cloudflare": "Cloudflare challenge", "login_redirect": "Redirect to login", "not_json": "Unexpected response",
    "zero_items": "Showed nothing — blocked or layout changed?", "mail_flags": "Could not label / move processed e-mails",
}


def _known(value: str | None) -> str:
    return "" if not value or value.strip().lower() in {"unknown", "n/a", "none"} else value.strip()


def _flag(code: str) -> str:
    return FLAGS.get(code, code.upper())


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
    icon: str = ""  # id for /icons/<id>: the source, or "mail-<key>" of the e-mail sender


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
    salary_rub: str  # "≈ 77 000 ₽" when the ad is in another currency and the rate is known
    published: str
    is_new: bool
    summary: str
    flags: list[str]
    sources: list[SourceLink]
    description: str
    user_status: str
    status_label: str
    skip_reason: str | None
    group: int | None
    run_id: int | None = None
    grade: str = ""
    employer_type: str = ""
    ad_language: str = ""          # flag (or code) of the language the ad is written in
    required_languages: list[str] = field(default_factory=list)  # flags with the language name as tooltip
    reports: list[dict[str, str]] = field(default_factory=list)  # debug mode: the user's complaints

    @property
    def low_fit(self) -> bool:
        return self.fit is not None and self.fit <= 2


class CardBuilder:
    def __init__(self, config: AppConfig, storage: Storage):
        self._sources: dict[str, SourceConfig] = {s.id: s for s in config.sources.sources}
        self._rates = RatesStore(config.paths)
        self._reports = storage.vacancies.reports()
        self._hours_per_month = config.rules.salary.hours_per_month
        self._sender_icons = sender_icons(config)
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
        if raw and (raw.meta.get("email") or {}).get("parser") == "linkedin" and not raw.detail_text and record.facts:
            # LinkedIn page not read (off / paused / limit): evaluated from the 1–2 lines of the letter only.
            flags.append("E-mail only")
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
            salary_rub=self._salary_rub(record),
            published=format_local(record.published_at, "%-d %b") if record.published_at else "",
            is_new=record.run_id == self._latest_run and record.user_status == "new",
            summary=_known(facts.summary_ru) if facts else "",
            flags=flags,
            sources=self._links(record),
            description=raw.full_text if raw else "",
            user_status=record.user_status,
            status_label=STATUS_LABELS.get(record.user_status, record.user_status),
            skip_reason=record.skip_reason,
            group=record.group_no,
            run_id=record.run_id,
            grade=GRADES.get(facts.seniority, "") if facts else "",
            employer_type=EMPLOYER_TYPES.get(facts.employer_type, "") if facts else "",
            ad_language=_flag(facts.ad_language) if facts and facts.ad_language != "other" else "",
            required_languages=[_flag(code) for code in facts.required_languages] if facts else [],
            reports=[{"text": r["text"], "date": format_local(r["created_at"], "%-d %b %H:%M")}
                     for r in self._reports.get(record.id, [])],
        )

    def _salary_rub(self, record: VacancyRecord) -> str:
        """Monthly RUB by the rates of the day the vacancy was collected; only for ads in another currency."""
        salary = parse_salary(record.facts.salary_text) if record.facts else None
        if salary is None or salary.currency in (None, "RUB"):
            return ""
        rates = self._rates.for_date(local(record.created_at).date() if record.created_at else None)
        low, high = (monthly_rub(v, salary, rates or {}, self._hours_per_month) for v in (salary.low, salary.high))
        if low is None and high is None:
            return ""
        if low is not None and high is not None:
            return f"≈ {format_rub(low).removesuffix(' ₽')}–{format_rub(high)}"
        return f"≈ {format_rub(low)}" if low is not None else f"≈ up to {format_rub(high)}"

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
            icon = source.source_id
            if kind == "email" and source.sender:
                name, icon = source.sender, self._sender_icons.get(source.sender) or source.source_id
            (channels if kind == "telegram" else sites).append(SourceLink(source.source_id, name, url, kind, icon))
        return sites + channels


def sender_icons(config: AppConfig) -> dict[str, str]:
    """Sender name → icon id. The first rule with an icon wins (several addresses share one sender name)."""
    icons: dict[str, str] = {}
    for rule in config.mail.senders:
        if rule.icon_id:
            icons.setdefault(rule.name, rule.icon_id)
    return icons


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
    linkedin: str = ""  # e-mail sources: how many LinkedIn vacancies got a description via the job API


def linkedin_descriptions(storage: Storage) -> dict[tuple[int, str], tuple[int, int]]:
    """(run, e-mail source) → (with a description from Google for Jobs, all LinkedIn vacancies of the letters)."""
    rows = storage.db.query(
        "SELECT run_id, source_id, COUNT(*) AS total,"
        " SUM(CASE WHEN detail_text LIKE '%Description source: Google for Jobs%' THEN 1 ELSE 0 END) AS found"
        " FROM raw_items WHERE staged = 0 AND external_id LIKE 'linkedin:%' GROUP BY run_id, source_id")
    return {(r["run_id"], r["source_id"]): (r["found"] or 0, r["total"]) for r in rows}


def linkedin_line(found: int, total: int, prefix: str = "LinkedIn descriptions") -> str:
    return f"{prefix}: {found} of {total} found via the job API ({round(100 * found / total)}%)" if total else ""


def linkedin_all_time(storage: Storage) -> dict[str, str]:
    """E-mail source → «All time: X of Y found via the job API (Z%)» for Statistics → Sources."""
    sums: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for (_run, source), (found, total) in linkedin_descriptions(storage).items():
        sums[source][0] += found
        sums[source][1] += total
    return {source: linkedin_line(found, total, "All time") for source, (found, total) in sums.items()}


@dataclass
class HealthRun:
    id: int
    started: str
    duration_min: str
    status: str
    summary: dict[str, Any]
    rows: list[HealthRow] = field(default_factory=list)
    problems: int = 0
    real_limit_pct: float | None = None  # typed by the user from claude.ai → Usage
    collected: int = 0  # new posts / letters / cards collected by all sources
    to_feed: int = 0    # of them, vacancies that went to the feed

    @property
    def network_mb(self) -> str:
        value = self.summary.get("network_bytes")
        return "" if value is None else f"{value / 1_000_000:.1f}"


def health(config: AppConfig, storage: Storage, limit: int = 60) -> list[HealthRun]:
    names = {s.id: s.name for s in config.sources.sources}
    runs = [r for r in storage.runs.history(limit)]
    described = linkedin_descriptions(storage)
    history: dict[str, list[int]] = defaultdict(list)
    result: list[HealthRun] = []
    for run in reversed(runs):
        started, finished = from_iso(run["started_at"]), from_iso(run["finished_at"])
        minutes = f"{(finished - started).total_seconds() / 60:.1f}" if started and finished else "…"
        health_run = HealthRun(run["id"], format_local(started), minutes, run["status"],
                               json.loads(run["summary_json"] or "{}"), real_limit_pct=run.get("real_limit_pct"))
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
                ALERT_LABELS.get(alert, alert), warn, linkedin_line(*described.get((run["id"], source_id), (0, 0))),
            ))
            health_run.problems += int(warn)
            health_run.collected += row["collected"] or 0
            health_run.to_feed += row["to_feed"] or 0
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


# ---------------------------------------------------------------- processed: filters and runs

NOT_ANALYSED = "not analysed"
NONE_VALUE = "none"


def _humanize(value: str) -> str:
    return value.replace("_", " ")


def _fact(name: str, labels: dict[str, str] | None = None) -> Any:
    def get(record: VacancyRecord) -> list[tuple[str, str]]:
        if record.facts is None:
            return [(NOT_ANALYSED, NOT_ANALYSED)]
        value = getattr(record.facts, name)
        values = value if isinstance(value, list) else [value]
        if isinstance(value, bool):
            values = ["yes" if value else "no"]
        out = [(str(v), (labels or {}).get(str(v), _humanize(str(v)))) for v in values if str(v).strip()]
        return out or [(NONE_VALUE, NONE_VALUE)]
    return get


def _sources(names: dict[str, str]) -> Any:
    """E-mail vacancies are listed by sender ("hh.ru"), everything else by source."""
    def get(record: VacancyRecord) -> list[tuple[str, str]]:
        values = {(f"{s.source_id}:{s.sender}", s.sender) if s.sender else (s.source_id, names.get(s.source_id, s.source_id))
                  for s in record.sources}
        return sorted(values) or [(NONE_VALUE, NONE_VALUE)]
    return get


def _plain(getter: Any) -> Any:
    def get(record: VacancyRecord) -> list[tuple[str, str]]:
        value = getter(record)
        return [(str(value), str(value))] if value not in (None, "") else [(NONE_VALUE, NONE_VALUE)]
    return get


def filter_fields(config: AppConfig) -> list[tuple[str, str, Any]]:
    """(query key, label, record → [(value, label)]). Everything the LLM extracts plus the code's verdict."""
    names = {s.id: s.name for s in config.sources.sources}
    return [
        ("reason", "Skip reason", _plain(lambda r: r.skip_reason)),
        ("source", "Source", _sources(names)),
        ("group", "Group", _plain(lambda r: r.group_no)),
        ("fit", "Fit", _plain(lambda r: r.stars)),
        ("permit", "Permit", _plain(lambda r: r.residence_permit)),
        ("ad_language", "Ad language", _fact("ad_language", LANGUAGES)),
        ("required_languages", "Required languages", _fact("required_languages", LANGUAGES)),
        ("serbian_required", "Serbian required", _fact("serbian_required")),
        ("english_level", "English level", _fact("english_level")),
        ("russian_speaking_team", "Russian-speaking team", _fact("russian_speaking_team")),
        ("country", "Country", _fact("country")),
        ("city", "City", _fact("city")),
        ("work_format", "Format", _fact("work_format")),
        ("remote_scope", "Remote scope", _fact("remote_scope")),
        ("role_category", "Role", _fact("role_category")),
        ("seniority", "Seniority", _fact("seniority")),
        ("stack_overlap", "Stack overlap", _fact("stack_overlap")),
        ("employer_type", "Employer type", _fact("employer_type")),
        ("serbian_entity", "Serbian entity", _fact("serbian_entity")),
        ("employment", "Employment", _fact("employment")),
        ("visa_support", "Visa support", _fact("visa_support")),
        ("confidence", "Confidence", _fact("confidence")),
    ]


@dataclass
class FilterOption:
    value: str
    label: str
    count: int
    selected: bool


@dataclass
class Filter:
    key: str
    label: str
    options: list[FilterOption]

    @property
    def selected(self) -> list[FilterOption]:
        return [o for o in self.options if o.selected]


FEED_MIN_STARS_DEFAULT = 4  # the feed shows ★5 and ★4 unless the filter says otherwise


def star_options(records: list[VacancyRecord], current: int) -> list[FilterOption]:
    """Feed «Fit» filter: one threshold — ★5 only, ★4+, ★3+, ★2+, All — with how many it shows."""
    labels = {5: "★5 only", 4: "★4 and 5", 3: "★3 and up", 2: "★2 and up", 0: "All"}
    return [FilterOption(str(n), label, sum(1 for r in records if (r.stars or 0) >= n), n == current)
            for n, label in labels.items()]


def apply_filters(config: AppConfig, records: list[VacancyRecord], chosen: dict[str, set[str]],
                  skip: set[str] = frozenset()) -> tuple[list[VacancyRecord], list[Filter]]:
    """OR inside one filter, AND between filters. Options are counted over the whole sub-tab."""
    fields = [f for f in filter_fields(config) if f[0] not in skip]
    values = {id(r): {key: get(r) for key, _, get in fields} for r in records}
    filters = []
    for key, label, _ in fields:
        counts: dict[str, int] = defaultdict(int)
        labels: dict[str, str] = {}
        for record in records:
            for value, value_label in values[id(record)][key]:
                counts[value] += 1
                labels[value] = value_label
        for value in chosen.get(key, set()) - counts.keys():
            counts[value], labels[value] = 0, value
        options = [FilterOption(v, labels[v], n, v in chosen.get(key, set()))
                   for v, n in sorted(counts.items(), key=(lambda kv: labels[kv[0]].lower()) if key == "source"
                                      else (lambda kv: (-kv[1], labels[kv[0]].lower())))]
        if options:
            filters.append(Filter(key, label, options))
    kept = [r for r in records
            if all(not wanted or wanted & {v for v, _ in values[id(r)][key]} for key, wanted in chosen.items())]
    return kept, filters


SEARCH_FIELDS = [("all", "Everywhere"), ("title", "Title"), ("employer", "Employer"), ("text", "Text"),
                 ("reason", "Skip reason")]


def search_records(storage: Storage, records: list[VacancyRecord], text: str, where: str) -> list[VacancyRecord]:
    """Case-insensitive substring search. "text" is the original ad (detail page if there is one), loaded with
    one query per 500 vacancies; "all" also looks at the title, employer, the Russian summary and the skip reason."""
    needle = text.strip().lower()
    if not needle:
        return records
    raw: dict[int, str] = {}
    if where in ("all", "text"):
        ids = sorted({r.primary_raw_id for r in records})
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            rows = storage.db.query(
                f"SELECT id, text, detail_text FROM raw_items WHERE id IN ({','.join('?' * len(chunk))})", tuple(chunk))
            raw.update({row["id"]: f"{row['text'] or ''}\n{row['detail_text'] or ''}".lower() for row in rows})

    def fields(record: VacancyRecord) -> list[str]:
        summary = record.facts.summary_ru if record.facts else ""
        by_name = {"title": [record.title], "employer": [record.employer], "reason": [record.skip_reason or ""],
                   "text": [raw.get(record.primary_raw_id, "")]}
        return by_name["title"] + by_name["employer"] + [summary] + by_name["reason"] + by_name["text"] \
            if where == "all" else by_name.get(where, [])
    return [r for r in records if any(needle in value.lower() for value in fields(r))]


def group_by_run(cards: list[Card], storage: Storage) -> list[dict[str, Any]]:
    runs = {r["id"]: r for r in storage.runs.history(1000)}
    by_run: dict[int | None, list[Card]] = defaultdict(list)
    for card in cards:
        by_run[card.run_id].append(card)
    result = []
    for run_id in sorted(by_run, key=lambda r: r or 0, reverse=True):
        run = runs.get(run_id) if run_id else None
        result.append({"run_id": run_id, "started": format_local(run["started_at"]) if run else "—",
                       "cards": by_run[run_id]})
    return result


# ---------------------------------------------------------------- statistics: tokens per run

def model_label(model: str) -> str:
    """claude-haiku-5-5 → Haiku 5.5, gpt-5.4-nano → GPT-5.4 nano."""
    if model.startswith("claude-"):
        family, *version = model.removeprefix("claude-").split("-")
        return f"{family.capitalize()} {'.'.join(version)}".strip()
    if model.startswith("gpt-"):
        return "GPT-" + model.removeprefix("gpt-").replace("-", " ", 1)
    return model


def _call_tokens(call: dict[str, Any]) -> int:
    return sum(call[k] or 0 for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens"))


FIVE_HOURS = 5 * 3600


def measured_limit_share(calls: list[dict[str, Any]], earlier: list[dict[str, Any]], started: datetime | None
                         ) -> tuple[float, bool] | None:
    """How much of the 5-hour limit a run took, from what Claude reports after every call (llm_calls.five_hour).
    Per 5-hour window: the last value in the run minus the value just before the run (the latest earlier call in
    the same window; 0 if the window began during the run). Unknown start → the run's first call is the base and the
    result is a lower bound (second value True). None — no call of the run carried the limits."""
    seen = [c for c in calls if c.get("five_hour") is not None and c.get("five_hour_resets_at")]
    if not seen:
        return None
    total, lower_bound = 0.0, False
    for resets in dict.fromkeys(c["five_hour_resets_at"] for c in seen):
        window = [c for c in seen if c["five_hour_resets_at"] == resets]
        before = [c for c in earlier if c.get("five_hour_resets_at") == resets and c.get("five_hour") is not None]
        if before:
            base = before[-1]["five_hour"]
        elif started and resets - FIVE_HOURS >= started.timestamp() - 60:
            base = 0.0  # the window opened with this run
        else:
            base, lower_bound = window[0]["five_hour"], True
        total += max(0.0, window[-1]["five_hour"] - base)
    return round(100 * total, 1), lower_bound


def tokens(config: AppConfig, storage: Storage) -> dict[str, Any]:
    """Totals for the Statistics header and tokens per run for the Runs table."""
    limit = config.llm.claude_sdk.pro_5h_limit_tokens if config.llm.provider == "claude_sdk" else None
    per_run: dict[int | None, int] = defaultdict(int)
    failed: dict[int | None, int] = defaultdict(int)
    calls = storage.llm_calls.all()
    runs = {r["id"]: r for r in storage.runs.history(1000)}
    measured: dict[int, tuple[float, bool] | None] = {}
    for run_id in {c["run_id"] for c in calls if c["run_id"] is not None}:
        own = [c for c in calls if c["run_id"] == run_id]
        first_id = own[0]["id"]
        measured[run_id] = measured_limit_share(own, [c for c in calls if c["id"] < first_id],
                                                from_iso(runs[run_id]["started_at"]) if run_id in runs else None)
    for call in calls:
        per_run[call["run_id"]] += _call_tokens(call)
        failed[call["run_id"]] += 0 if call["ok"] else 1
    total = sum(per_run.values())
    return {
        "total": total, "calls": len(calls), "pro_limit": limit, "provider": config.llm.provider,
        "auth_source": storage.llm_calls.last_auth_source(),
        "per_run": {run_id: {"tokens": n, "failed": failed[run_id],
                             "limit_share": round(100 * n / limit, 1) if limit else None,
                             "measured": measured.get(run_id)}
                    for run_id, n in per_run.items()},
    }


def claude_limits(config: AppConfig) -> dict[str, Any] | None:
    """Footer bars like Claude's own: «5-hour limit … Resets in 2 h 10 min  58%», «Weekly · all models … 37%»."""
    if config.llm.provider != "claude_sdk":
        return None
    from ..llm import limits

    saved = limits.load(config.paths.data / "claude_limits.json")
    if not saved:
        return {"bars": [], "updated": "never — press ↻ (one tiny Claude call)"}
    now = datetime.now(UTC).timestamp()
    bars = []
    for key, label in limits.WINDOWS:
        window = (saved.get("windows") or {}).get(key) or {}
        resets = window.get("resetsAt")
        used = float(window.get("utilization") or 0) if resets and resets > now else 0.0
        if not resets or resets <= now:
            note = "Starts with your first message" if key == "five_hour" else "Resets soon"
        elif key == "five_hour":
            minutes = int((resets - now) // 60)
            note = f"Resets in {minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"Resets in {minutes} min"
        else:
            note = "Resets " + format_local(datetime.fromtimestamp(resets, UTC), "%a %H:%M")
        bars.append({"label": label, "note": note, "percent": round(used * 100)})
    ago = int((now - saved.get("observed_at", now)) // 60)
    updated = "just now" if ago < 1 else f"{ago} min ago" if ago < 120 else f"{ago // 60} h ago"
    return {"bars": bars, "updated": updated}


STEP_SHORT = {"note": "note", "folder": "Drive folder", "materials": "CV + letter (Claude)",
              "skills": "skills (Claude)", "pdf": "PDF", "upload": "upload", "finalize": "finish"}


def apply_stats(config: AppConfig, storage: Storage) -> list[dict[str, Any]]:
    """Statistics → Applies: one row per ▶ / Not now — time of every step and the Claude calls with their tokens.
    Calls made before 30.10.2026 were not tied to an application and are not counted here."""
    limit = config.llm.claude_sdk.pro_5h_limit_tokens if config.llm.provider == "claude_sdk" else None
    calls = storage.llm_calls.by_application()
    rows = []
    for app in storage.applications.history():
        record = storage.vacancies.get(app.vacancy_id)
        steps = app.stats.get("steps", {})
        own = calls.get(app.id, [])
        tokens = sum(_call_tokens(c) for c in own)
        rows.append({
            "started": format_local(app.created_at), "kind": "Not now" if app.kind == "not_now" else "Apply",
            "title": (record.facts.display_title if record and record.facts else "") or (record.title if record else "?"),
            "employer": record.employer if record else "", "state": app.state.value,
            "seconds": round(sum(steps.values())) if steps else None,
            "steps": " · ".join(f"{STEP_SHORT.get(k, k)} {v:.0f}s" for k, v in steps.items()),
            "calls": len(own), "failed": sum(1 for c in own if not c["ok"]), "tokens": tokens,
            "models": ", ".join(sorted({model_label(c["model"]) for c in own if c["model"]})),
            "limit_share": round(100 * tokens / limit, 1) if limit and own else None,
        })
    return rows


# ---------------------------------------------------------------- debug mode

def debug_info(config: AppConfig, storage: Storage, vacancy_id: int) -> dict[str, Any] | None:
    """Everything needed to discuss one vacancy: verdict, the rule trace, LLM facts and raw items."""
    from ..scoring.engine import ScoringEngine
    from ..timeutil import utcnow

    record = storage.vacancies.get(vacancy_id)
    if record is None:
        return None
    if record.facts is not None:
        collected = local(record.created_at).date() if record.created_at else None
        explain = ScoringEngine(config.rules).explain(record.facts, record.published_at, utcnow(),
                                                      RatesStore(config.paths).for_date(collected))
        facts = record.facts.model_dump()
    else:
        explain = [f"Skipped before the LLM (no facts): {record.skip_reason or '—'}"]
        facts = None
    raw = storage.vacancies.raw_links(vacancy_id)
    names = {s.id: s.name for s in config.sources.sources}
    raw_lines = [f"raw {r['id']} · {names.get(r['source_id'], r['source_id'])} #{r['external_id']} · {r['status']}"
                 + (f" (duplicate of raw {r['duplicate_of']})" if r["duplicate_of"] else "")
                 + (f" · {r['url']}" if r["url"] else "") for r in raw]
    verdict = (f"group {record.group_no or '—'} · fit {record.stars or '—'} · permit {record.residence_permit or '—'}"
               f" · decision {record.decision}" + (f" ({record.skip_reason})" if record.skip_reason else "")
               + f" · status {record.user_status}")
    reports = storage.vacancies.reports().get(record.id, [])
    report = "\n".join([
        f"Vacancy id {record.id}: {record.title}" + (f" — {record.employer}" if record.employer else ""),
        f"Run {record.run_id} · {verdict}",
        *[f"User report {format_local(r['created_at'], '%-d %b %H:%M')}: {r['text']}" for r in reports],
        "", "Why:", *[f"- {line}" for line in explain],
        "", "Raw items:", *[f"- {line}" for line in raw_lines],
        "", "Facts:", json.dumps(facts, ensure_ascii=False, indent=2) if facts else "none (not analysed)",
    ])
    return {"id": record.id, "verdict": verdict, "explain": explain, "raw": raw_lines,
            "facts": json.dumps(facts, ensure_ascii=False, indent=2) if facts else "", "report": report}


def hidden_items(config: AppConfig, storage: Storage) -> list[dict[str, Any]]:
    names = {s.id: s.name for s in config.sources.sources}
    runs = {r["id"]: r for r in storage.runs.history(1000)}
    by_run: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item, reason in storage.items.hidden():
        text = " ".join(item.full_text.split())
        by_run[item.run_id].append({"id": item.id, "source": names.get(item.source_id, item.source_id),
                                    "source_id": item.source_id, "url": item.url, "reason": reason,
                                    "text": text[:220] + ("…" if len(text) > 220 else ""),
                                    "published": format_local(item.published_at, "%-d %b") if item.published_at else ""})
    return [{"run_id": run_id, "started": format_local(runs[run_id]["started_at"]) if run_id in runs else "—",
             "rows": rows} for run_id, rows in sorted(by_run.items(), reverse=True)]


STEP_LABELS = {
    "note": "Writing the Obsidian note…",
    "folder": "Creating the Drive folder…",
    "materials": "Tailoring CV & cover letter (Claude)…",
    "skills": "Picking the vacancy's skills (Claude)…",
    "pdf": "Building the PDF…",
    "upload": "Uploading the CV to Drive…",
    "finalize": "Finishing…",
}


def application_item(storage: Storage, application: Application) -> dict[str, Any]:
    """One row of the Apply sidebar."""
    record = storage.vacancies.get(application.vacancy_id)
    state = application.state.value
    if state == "queued":
        status = "Queued" if not application.started else "Continuing…"
    elif state == "working" and application.cancel_requested:
        status = "Stopping after this step…"
    elif state == "working":
        status = STEP_LABELS.get(application.step or "", "Working…")
    elif state == "waiting":
        status = f"Claude limit — retry at {format_local(application.due_at, '%H:%M')}"
    elif state == "failed":
        status = f"Stopped at “{application.step}”"
    elif state == "cancelled":
        status = "Stopped"
    else:
        status = "Ready" if application.kind == "apply" else "Obsidian note ready"
    if application.kind == "not_now":
        status = f"Not now · {status}"
    return {
        "id": application.id, "vacancy_id": application.vacancy_id,
        "title": (record.facts.display_title if record and record.facts else "") or (record.title if record else "?"),
        "employer": record.employer if record else "",
        "state": state, "active": application.state.active, "status": status,
        "error": application.error, "warning": application.warning,
        "has_pdf": bool(application.pdf_path and Path(application.pdf_path).exists()),
        # the user deleted the PDF from Downloads: «Rebuild PDF» makes it again without LLM
        "pdf_removed": bool(state == "done" and application.pdf_path
                            and not Path(application.pdf_path).exists()), "has_letter": bool(application.letter),
        "note_url": f"obsidian://open?path={quote(application.note_path)}" if application.note_path else None,
        "folder_url": application.folder_url, "pdf_url": application.pdf_url,
    }
