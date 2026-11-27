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
from ..timeutil import day_start, format_local, from_iso, local, utcnow

LANGUAGES = {
    "ru": "Russian", "en": "English", "sr": "Serbian", "de": "German", "fr": "French", "es": "Spanish",
    "it": "Italian", "pl": "Polish", "uk": "Ukrainian", "kk": "Kazakh", "ky": "Kyrgyz", "nl": "Dutch",
    "pt": "Portuguese", "tr": "Turkish", "zh": "Chinese", "ja": "Japanese", "cs": "Czech",
}
WORK_FORMATS = {"office": "Office", "hybrid": "Hybrid", "remote": "Remote"}
RUSSIAN_TAG, ENGLISH_TAG = "Russian-speaking", "English team"
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


def _language(code: str) -> str:
    return LANGUAGES.get(code, code.upper())


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


@dataclass(frozen=True)
class Flag:
    """A tag after the title. kind → colour in style.css (.flag-<kind>); title → the tooltip."""

    text: str
    kind: str = "plain"
    title: str = ""

    def __eq__(self, other: object) -> bool:  # tests and templates compare with the text
        return self.text == other if isinstance(other, str) else super().__eq__(other)

    def __hash__(self) -> int:
        return hash(self.text)


EMAIL_ONLY_TITLE = ("Evaluated from the job-alert letter only (title, company, city): no description was read — "
                    "less reliable, at most 4 stars")
WAVE_TITLE = "The letter announces an internship wave / open recruitment, not one vacancy"
ACCREDITATION_FLAGS = {
    "yes": ("IT accredited", "ok"), "no": ("No IT accreditation", "muted"),
    "unknown": ("IT accreditation unknown", "orange"), None: ("IT accreditation not checked", "plain"),
}


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
    flags: list[Flag]
    sources: list[SourceLink]
    description: str
    user_status: str
    status_label: str
    skip_reason: str | None
    group: int | None
    run_id: int | None = None
    grade: str = ""
    employer_type: str = ""
    ad_language: str = ""          # name of the language the ad is written in
    required_languages: list[str] = field(default_factory=list)  # language names
    reports: list[dict[str, str]] = field(default_factory=list)  # debug mode: the user's complaints
    run_started: str = ""          # debug mode: the run that collected it
    description_note: str = ""     # debug mode: where the description came from and how much of it the LLM reads
    status_changed_at: datetime | None = None
    created_at: datetime | None = None
    last_report_at: datetime | None = None

    @property
    def summary_lines(self) -> list[tuple[str, str]]:
        lines = []
        for line in self.summary.splitlines():
            line = line.strip()
            if not line:
                continue
            label, _, value = line.partition(":")
            if label in ("Кто Что", "Опыт", "Стэк") and value.strip():
                lines.append((label, value.strip()))
            else:
                lines.append(("", line))
        return lines

    def acted_at(self, tab: str | None) -> datetime | None:
        """When the user did what put the card on this tab (old cards may have no date: when it was collected)."""
        status_date = self.status_changed_at if self.user_status != "new" else None
        if tab == "reported":
            return self.last_report_at or status_date or self.created_at
        return status_date or self.last_report_at or self.created_at

    @property
    def low_fit(self) -> bool:
        return self.fit is not None and self.fit <= 2


class CardBuilder:
    def __init__(self, config: AppConfig, storage: Storage):
        self._sources: dict[str, SourceConfig] = {s.id: s for s in config.sources.sources}
        self._rates = RatesStore(config.paths)
        self._reports = storage.vacancies.reports()
        self._hours_per_month = config.rules.salary.hours_per_month
        self._rules = config.rules
        self._sender_icons = sender_icons(config)
        self._storage = storage
        self._llm = config.llm
        self._runs = {r["id"]: r["started_at"] for r in storage.runs.history(1000)}
        latest = storage.runs.last_finished()
        self._latest_run = latest["id"] if latest and latest["status"] == "committed" else None

    def build(self, record: VacancyRecord) -> Card:
        facts = record.facts
        raw = self._storage.items.get(record.primary_raw_id)
        flags: list[Flag] = []
        if facts and facts.wave:
            flags.append(Flag("Internship wave", "wave", WAVE_TITLE))
        if facts and facts.russian_link:
            if record.group_no not in self._rules.russian_tag_hidden_groups:
                flags.append(Flag(RUSSIAN_TAG, "ru"))
        elif facts and facts.english_team:
            flags.append(Flag(ENGLISH_TAG, "en"))
        if facts and self._accreditation_flag(record):
            text, kind = ACCREDITATION_FLAGS[facts.accreditation]
            note = facts.accreditation_note or ("Not checked yet: the check runs after each run"
                                                if facts.accreditation is None else "")
            flags.append(Flag(text, kind, note))
        if record.informal_flag:
            flags.append(Flag("Informal"))
        if raw and raw.email_only and record.facts:  # evaluated from the 1–2 lines of a job-alert letter only
            flags.append(Flag("E-mail only", "plain", EMAIL_ONLY_TITLE))
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
            description_note=self._description_note(raw) if raw else "",
            user_status=record.user_status,
            status_label=STATUS_LABELS.get(record.user_status, record.user_status),
            skip_reason=record.skip_reason,
            group=record.group_no,
            run_id=record.run_id,
            run_started=format_local(self._runs[record.run_id]) if record.run_id in self._runs else "",
            status_changed_at=record.status_changed_at,
            created_at=record.created_at,
            last_report_at=max((from_iso(r["created_at"]) for r in self._reports.get(record.id, [])), default=None),
            grade=GRADES.get(facts.seniority, "") if facts else "",
            employer_type=EMPLOYER_TYPES.get(facts.employer_type, "") if facts else "",
            ad_language=_language(facts.ad_language) if facts and facts.ad_language != "other" else "",
            required_languages=[_language(code) for code in facts.required_languages] if facts else [],
            reports=[{"text": r["text"], "date": format_local(r["created_at"], "%-d %b %H:%M")}
                     for r in self._reports.get(record.id, [])],
        )

    def _accreditation_flag(self, record: VacancyRecord) -> bool:
        """Only vacancies of the groups whose employers are checked (Russia) and of the no-accreditation group."""
        rules = self._rules
        return bool(rules.it_accreditation_groups) and (
            record.group_no in rules.it_accreditation_groups or record.group_no == rules.no_it_accreditation_group)

    def _description_note(self, raw: Any) -> str:
        """«From the vacancy's page · 5 207 characters · the LLM reads the first 4 000»."""
        if raw.meta.get("email"):
            origin = "Full description found by Google for Jobs" if raw.detail_text else "The job-alert letter only"
        else:
            origin = "From the vacancy's page" if raw.detail_text else "The post / card text"
        llm = self._llm
        limit = llm.max_input_chars_multi if raw.multi else llm.max_input_chars
        size = len(raw.full_text)
        read = f"the LLM reads the first {limit:,}".replace(",", " ") if size > limit else "the LLM reads all of it"
        return f"{origin} · {size:,} characters · {read}".replace(",", " ")

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





def group_feed(cards: list[Card], config: AppConfig) -> list[dict[str, Any]]:
    if cards and all(card.is_new for card in cards):
        for card in cards:
            card.is_new = False
    by_group: dict[int, list[Card]] = defaultdict(list)
    for card in cards:
        by_group[card.group or 5].append(card)
    groups = []
    for number in config.rules.enabled_groups:
        info = config.rules.groups[number]
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
    linkedin: DescriptionSplit | None = None  # e-mail sources: where the LinkedIn vacancies got their description


@dataclass
class SplitPart:
    key: str
    label: str
    count: int
    percent: int


@dataclass
class DescriptionSplit:
    """LinkedIn vacancies of the letters by where their description came from (Statistics → Sources / Runs)."""

    google: int = 0
    page: int = 0
    none: int = 0
    waiting: int = 0

    LABELS = (("google", "Google for Jobs"), ("page", "LinkedIn page"), ("none", "Not found"),
              ("waiting", "Waiting for the page"))

    @property
    def total(self) -> int:
        return self.google + self.page + self.none + self.waiting

    def __add__(self, other: DescriptionSplit) -> DescriptionSplit:
        return DescriptionSplit(self.google + other.google, self.page + other.page, self.none + other.none,
                                self.waiting + other.waiting)

    @property
    def parts(self) -> list[SplitPart]:
        total = self.total or 1
        return [SplitPart(key, label, getattr(self, key), round(100 * getattr(self, key) / total))
                for key, label in self.LABELS if getattr(self, key)]


def linkedin_descriptions(storage: Storage) -> dict[tuple[int, str], DescriptionSplit]:
    """(run, e-mail source) → where the descriptions of that run's LinkedIn vacancies came from (current state:
    a vacancy waiting for its page in one run is counted as found / not found once a later run has read it)."""
    rows = storage.db.query(
        "SELECT run_id, source_id, COUNT(*) AS total,"
        " SUM(CASE WHEN status = 'waiting_page' THEN 1 ELSE 0 END) AS waiting,"
        " SUM(CASE WHEN status != 'waiting_page' AND detail_text LIKE '%Description source: Google for Jobs%'"
        " THEN 1 ELSE 0 END) AS google,"
        " SUM(CASE WHEN status != 'waiting_page' AND detail_text LIKE '%Description source: LinkedIn public page%'"
        " THEN 1 ELSE 0 END) AS page"
        " FROM raw_items WHERE staged = 0 AND external_id LIKE 'linkedin:%' AND status IN ('scored', 'waiting_page')"
        " GROUP BY run_id, source_id")
    return {(r["run_id"], r["source_id"]): DescriptionSplit(
        r["google"] or 0, r["page"] or 0, r["total"] - (r["google"] or 0) - (r["page"] or 0) - (r["waiting"] or 0),
        r["waiting"] or 0) for r in rows}


def linkedin_all_time(storage: Storage) -> dict[str, DescriptionSplit]:
    sums: dict[str, DescriptionSplit] = defaultdict(DescriptionSplit)
    for (_run, source), split in linkedin_descriptions(storage).items():
        sums[source] = sums[source] + split
    return dict(sums)


@dataclass
class HealthRun:
    id: int
    started: str
    duration_min: str
    status: str
    summary: dict[str, Any]
    rows: list[HealthRow] = field(default_factory=list)
    problems: int = 0
    real_limit_pct: float | None = None  # typed by hand before 31.10.2026; shown in «% of 5h from CLAUDE»
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
                ALERT_LABELS.get(alert, alert), warn, described.get((run["id"], source_id)),
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
    icon: str = ""  # Source filter: the id for /icons/<id> (the source, or the e-mail sender's icon)


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
    known = {key for key, _, _ in fields}
    chosen = {key: wanted for key, wanted in chosen.items() if key in known}  # an old bookmark's ?fit= must not fail
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
        icons = sender_icons(config) if key == "source" else {}
        options = [FilterOption(v, labels[v], n, v in chosen.get(key, set()),
                                (icons.get(labels[v], v.split(":", 1)[0]) if ":" in v else v) if key == "source" else "")
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
        result.append({"title": f"Run {format_local(run['started_at'])}" if run else "Run —", "cards": by_run[run_id]})
    return result


def group_by_action_day(cards: list[Card], tab: str | None) -> list[dict[str, Any]]:
    """Newest first: the days the user acted on these cards, and the cards of each day by time."""
    oldest = datetime.min.replace(tzinfo=UTC)
    days: dict[str, list[Card]] = {}
    for card in sorted(cards, key=lambda c: c.acted_at(tab) or oldest, reverse=True):
        days.setdefault(format_local(card.acted_at(tab), "%-d %b %Y") or "—", []).append(card)
    return [{"title": day, "cards": rows} for day, rows in days.items()]


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
    limit = config.llm.claude_sdk.pro_5h_limit_tokens
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
    non_claude_runs = {c["run_id"] for c in calls if c["provider"] != "claude_sdk"}
    total = sum(per_run.values())
    return {
        "total": total, "calls": len(calls), "pro_limit": limit, "provider": config.llm.provider,
        "auth_source": storage.llm_calls.last_auth_source(),
        "per_run": {run_id: {"tokens": n, "failed": failed[run_id],
                             "limit_share": round(100 * n / limit, 1) if limit and run_id not in non_claude_runs else None,
                             "measured": measured.get(run_id)}
                    for run_id, n in per_run.items()},
    }




STEP_SHORT = {"note": "note", "folder": "Drive folder", "materials": "CV + letter",
              "skills": "skills", "pdf": "PDF", "upload": "upload", "finalize": "finish"}


def apply_stats(config: AppConfig, storage: Storage) -> list[dict[str, Any]]:
    """Statistics → Applies: one row per ▶ / Not now — time of every step and the Claude calls with their tokens.
    Calls made before 30.10.2026 were not tied to an application and are not counted here."""
    limit = config.llm.claude_sdk.pro_5h_limit_tokens
    calls = storage.llm_calls.by_application()
    rows = []
    for app in storage.applications.history():
        record = storage.vacancies.get(app.vacancy_id)
        steps = app.stats.get("steps", {})
        own = calls.get(app.id, [])
        tokens = sum(_call_tokens(c) for c in own)
        step_names = STEP_SHORT.copy()
        providers = {c["provider"] for c in own}
        name = {"claude_sdk": "Claude", "codex_cli": "Codex"}.get(next(iter(providers))) if len(providers) == 1 else None
        if name:
            for key in ("materials", "skills"):
                step_names[key] += f" ({name})"
        rows.append({
            "started": format_local(app.created_at), "kind": "Not now" if app.kind == "not_now" else "Apply",
            "title": (record.facts.display_title if record and record.facts else "") or (record.title if record else "?"),
            "employer": record.employer if record else "", "state": app.state.value,
            "seconds": round(sum(steps.values())) if steps else None,
            "steps": " · ".join(f"{step_names.get(k, k)} {v:.0f}s" for k, v in steps.items()),
            "calls": len(own), "failed": sum(1 for c in own if not c["ok"]), "tokens": tokens,
            "models": ", ".join(sorted({model_label(c["model"]) for c in own if c["model"]})),
            "limit_share": round(100 * tokens / limit, 1) if limit and own and all(c["provider"] == "claude_sdk" for c in own) else None,
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
        primary = storage.items.get(record.primary_raw_id)
        explain = ScoringEngine(config.rules).explain(record.facts, record.published_at, utcnow(),
                                                      RatesStore(config.paths).for_date(collected),
                                                      bool(primary and primary.email_only))
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
    return {"id": record.id, "verdict": verdict, "explain": explain, "raw": raw_lines,
            "facts": json.dumps(facts, ensure_ascii=False, indent=2) if facts else ""}


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
    "skills": "Picking the vacancy's skills…",
    "pdf": "Building the PDF…",
    "upload": "Uploading the CV to Drive…",
    "finalize": "Finishing…",
}


def _card_url(record: VacancyRecord | None) -> str | None:
    """The sidebar's title opens the vacancy's card on the tab where it is now."""
    if record is None:
        return None
    tab = record.user_status if record.user_status in ("applied", "not_now", "skipped", "auto_skipped") else None
    return f"/processed?filter={tab}#vac-{record.id}" if tab else f"/processed#vac-{record.id}"


def application_item(config: AppConfig, storage: Storage, application: Application,
                     now: datetime | None = None) -> dict[str, Any]:
    """One row of the Apply sidebar. Hands-on work (kind: manual) has no CV, so no PDF buttons, even on old rows."""
    record = storage.vacancies.get(application.vacancy_id)
    manual = is_manual(config, record)
    state = application.state.value
    if state == "draft":
        status = "Choose what to make"
    elif state == "queued":
        status = "Queued" if not application.started else "Continuing…"
    elif state == "working" and application.cancel_requested:
        status = "Stopping after this step…"
    elif state == "working":
        if application.step == "materials" and (manual or not application.with_cv):
            status = "Writing the cover letter…"
        elif application.step == "pdf" and application.provided_pdf:
            status = "Preparing the selected PDF…"
        else:
            status = STEP_LABELS.get(application.step or "", "Working…")
    elif state == "waiting":
        status = f"Claude limit — retry at {format_local(application.due_at, '%H:%M')}"
    elif state == "failed":
        status = f"Stopped at “{application.step}”"
    elif state == "cancelled":
        status = "Stopped"
    else:  # done: what it became (green Applied, grey Not now)
        status = "Applied" if application.kind == "apply" else "Not now"
    if application.kind == "not_now" and state != "done":
        status = f"Not now · {status}"
    return {
        "id": application.id, "vacancy_id": application.vacancy_id,
        "vacancy_status": record.user_status if record else None,
        "card_url": _card_url(record),
        "title": (record.facts.display_title if record and record.facts else "") or (record.title if record else "?"),
        "employer": record.employer if record else "",
        "state": state, "active": application.state.active, "status": status,
        "error": application.error, "warning": application.warning,
        "has_pdf": bool(not manual and application.pdf_path and Path(application.pdf_path).exists()),
        # the user deleted the PDF from Downloads: «Rebuild PDF» makes it again without LLM
        "pdf_removed": bool(not manual and state == "done" and application.pdf_path and not application.provided_pdf
                            and not Path(application.pdf_path).exists()), "has_letter": bool(application.letter),
        "note_url": f"obsidian://open?path={quote(application.note_path)}" if application.note_path else None,
        "folder_url": application.folder_url, "pdf_url": application.pdf_url,
        "kind": application.kind, "draft": state == "draft", "manual": manual, "with_cv": application.with_cv,
        "with_letter": application.with_letter,
        "section": sidebar_section(config, application, now or utcnow()),
    }


def is_manual(config: AppConfig, record: VacancyRecord | None) -> bool:
    """Hands-on work (kind: manual in rules.yaml): a cover letter, never a CV."""
    rule = config.rules.categories.get(record.category) if record else None
    return bool(rule and rule.kind == "manual")


# The visible sidebar sections, top to bottom.
SIDEBAR_SECTIONS = (
    {"key": "purgatory", "title": "Purgatory", "current": True},
    {"key": "generated", "title": "Generated today", "current": True},
)


def sidebar_section(config: AppConfig, application: Application, now: datetime) -> str:
    """«▶» / N before today's start (apply.yaml → day_starts_at, local time): «previous» (work in progress stays up);
    otherwise «generated» once done («▶» and N alike), «purgatory» while waiting for Generate, in work, stopped or
    failed."""
    in_work = application.state.active
    if not in_work and application.created_at is not None and \
            application.created_at < day_start(now, config.apply.day_starts_at):
        return "previous"
    return "generated" if application.state.value == "done" else "purgatory"


def sidebar_sections(config: AppConfig, storage: Storage, applications: list[Application]) -> list[dict[str, Any]]:
    """The sections with their rows, in order; an empty section is left out."""
    now = utcnow()
    items = [application_item(config, storage, a, now) for a in applications]
    sections = [{**s, "items": [i for i in items if i["section"] == s["key"]]} for s in SIDEBAR_SECTIONS]
    return [s for s in sections if s["items"]]


# ---------------------------------------------------------------- statuses of applications (mail triage)

def _mail_link(source: SourceConfig | None, config: AppConfig, message_id: str) -> str:
    """Gmail opens a letter by its Message-ID (search rfc822msgid:); Yandex Mail has no such link — nothing."""
    if not source or not message_id:
        return ""
    mailbox_id = source.params.get("mailbox", source.id)
    mailbox = next((m for m in config.mail.mailboxes if m.id == mailbox_id), None)
    if mailbox is None or mailbox.kind != "gmail":
        return ""
    return "https://mail.google.com/mail/u/0/#search/" + quote(f"rfc822msgid:{message_id.strip('<>')}")


def status_sections(config: AppConfig, storage: Storage) -> list[dict[str, Any]]:
    """The Statuses tab: «New» (not seen on this tab yet) above «Seen». Newest letters first."""
    from ..pipeline.mail_stage import STAGES

    sources = {s.id: s for s in config.sources.sources}
    new, seen = [], []
    for record in storage.statuses.visible():
        label, tone = STAGES.get(record.stage, STAGES["unclear"])
        source = sources.get(record.source_id)
        when = record.received_at or record.created_at
        row = {
            "id": record.id, "stage": record.stage, "label": label, "tone": tone, "unsure": record.unsure,
            "company": record.company, "position": record.position, "summary": record.summary,
            "subject": record.subject, "sender": record.sender, "mailbox": source.name if source else record.source_id,
            "date": format_local(when, "%-d %b %H:%M") if when else "", "link": _mail_link(source, config,
                                                                                              record.message_id),
            "new": record.seen_at is None,
        }
        (new if row["new"] else seen).append(row)
    return [section for section in (
        {"key": "new", "title": "New", "rows": new},
        {"key": "seen", "title": "Seen", "rows": seen},
    ) if section["rows"]]
