"""After a run is committed: the IT accreditation of employers of the feed's Russian vacancies.

Runs after the commit (like the mail flags): the vacancies are already visible rows, their facts get
`it_accreditation` and their verdict is recalculated (pipeline/recalculate.reevaluate) — a vacancy whose employer
surely has no accreditation moves to rules.yaml → no_it_accreditation_group. A failure here costs nothing: the
vacancies keep «not checked» (shown as unknown) and are checked in the next run.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..collectors.http import PoliteHttpClient
from ..config import AppConfig
from ..llm.base import LlmError, LlmProvider
from ..storage import Storage
from ..storage.vacancies import VacancyRecord
from .dadata import DaData
from .names import inn_digits, valid_inn
from .registry import Registry, RegistryRecord, RegistryStore
from .resolver import YES, AccreditationResolver, EmployerQuery, known_employer, unknown_record

log = logging.getLogger(__name__)


@dataclass
class AccreditationSummary:
    checked: int = 0          # vacancies that got a status in this pass
    employers_asked: int = 0  # employers sent to the LLM
    moved: int = 0            # vacancies whose group / decision changed
    problem: str | None = None


def registry_store(config: AppConfig) -> RegistryStore:
    settings = config.accreditation
    return RegistryStore(config.paths.data / "it_registry", settings.listing_url, settings.refresh_days)


def candidates(config: AppConfig, storage: Storage) -> list[VacancyRecord]:
    """Feed vacancies in the checked groups and the no-accreditation group. All of them, not only the unchecked: a
    new register snapshot can change «yes» / «no» (re-decided from the cache, without the LLM)."""
    rules = config.rules
    groups = set(rules.it_accreditation_groups) | ({rules.no_it_accreditation_group} - {None})
    return [r for r in storage.vacancies.feed() if r.facts is not None and r.group_no in groups]


def _hints(record: VacancyRecord, storage: Storage) -> dict[str, str]:
    facts = record.facts
    raw = storage.items.get(record.primary_raw_id)
    text = (raw.full_text if raw else "")[:600]
    hints = {"title": record.title, "city": facts.city if facts else "", "link": facts.link if facts else "",
             "text": text}
    return {k: v for k, v in hints.items() if v and v != "unknown"}


def _register_hit(record: VacancyRecord, storage: Storage, registry: Registry) -> RegistryRecord | None:
    """The employer's own INN from the source (trudvsem gives it), when that entity is active in the register.
    Not active / absent → the usual check by name: an IT company of the same group may still be accredited."""
    raw = storage.items.get(record.primary_raw_id)
    inn = inn_digits(str((raw.meta or {}).get("employer_inn") or "")) if raw else ""
    if not inn or not valid_inn(inn):
        return None
    hit = registry.find_inn(inn)
    return hit if hit is not None and hit.active else None


def run_accreditation(config: AppConfig, storage: Storage, provider_factory: Callable[[], LlmProvider] | None,
                      http: PoliteHttpClient, cancel_event: threading.Event, run_id: int | None = None,
                      records: list[VacancyRecord] | None = None,
                      on_progress: Callable[[str], None] = lambda text: None,
                      recheck_unknown: bool = False) -> AccreditationSummary:
    from ..pipeline.llm_stage import LlmCaller, run_jobs
    from ..pipeline.recalculate import reevaluate

    summary = AccreditationSummary()
    settings = config.accreditation
    if not settings.enabled or not config.rules.it_accreditation_groups:
        return summary
    records = candidates(config, storage) if records is None else records
    if not records:
        return summary
    store = registry_store(config)
    summary.problem = store.refresh(http)
    registry = store.load()
    if registry is None:
        summary.problem = summary.problem or "IT register: no snapshot in data/it_registry/"
        return summary
    on_progress("Checking IT accreditation of employers")

    by_key: dict[str, list[VacancyRecord]] = {}
    queries: dict[str, EmployerQuery] = {}
    no_name: list[VacancyRecord] = []
    by_inn: list[VacancyRecord] = []
    for record in records:
        if record.facts is not None and record.facts.no_accreditation_sign:
            continue  # the vacancy itself says there is none (rules.yaml → no_it_accreditation_signs): no LLM, no register
        hit = _register_hit(record, storage, registry)
        if hit is not None:  # the source gave the employer's INN (trudvsem) and it is active in the register
            if _set(record, YES, f"{hit.label()} (ИНН из вакансии)"):
                storage.vacancies.set_facts(record.id, record.facts)
                by_inn.append(record)
            continue
        name = record.employer or (record.facts.employer_name if record.facts else "")
        if not known_employer(name):
            no_name.append(record)
            continue
        query = EmployerQuery(name.strip(), _hints(record, storage))
        by_key.setdefault(query.key, []).append(record)
        queries.setdefault(query.key, query)

    caller = None
    provider: LlmProvider | None = None
    if provider_factory is not None:
        try:
            provider = provider_factory()
        except LlmError as exc:
            summary.problem = f"IT accreditation: {exc}"
    if provider is not None:
        caller = LlmCaller(provider, storage.llm_calls, run_id, config.llm.retries, config.paths.data / "llm_failures")
    models = config.llm.stage_models
    model = models.accreditation or models.dedup

    async def ask(system: str, user: str, n_items: int) -> dict[str, Any]:
        from .resolver import SCHEMA

        assert caller is not None
        response = await caller.call("accreditation", system, user, model, n_items, SCHEMA)
        return response.data

    key = config.secrets.get(settings.dadata_key_env)
    dadata = DaData(key) if key else None
    try:
        resolver = AccreditationResolver(config, storage.accreditation, registry, ask if caller else None, dadata)
        cached = resolver.cached(list(queries))
        asking = [q for k, q in queries.items() if resolver.needs_llm(cached.get(k), recheck_unknown)][: settings.max_employers_per_run]
        if asking and caller is not None:
            batches = resolver.batches(asking)
            summary.employers_asked = len(asking)
            results = run_jobs([lambda b=b: resolver.resolve(b) for b in batches], config.llm.max_concurrency,
                               cancel_event)
            for result in results:
                if isinstance(result, BaseException):
                    summary.problem = f"IT accreditation: {result}"
                    continue
                for record in result:
                    cached[record.key] = record
    finally:
        if dadata is not None:
            dadata.close()

    changed: list[VacancyRecord] = list(by_inn)
    for key_, vacancies in by_key.items():
        result = cached.get(key_)
        if result is None:
            continue
        for vacancy in vacancies:
            if _set(vacancy, result.status, result.note):
                storage.vacancies.set_facts(vacancy.id, vacancy.facts)
                changed.append(vacancy)
    for vacancy in no_name:
        record = unknown_record(EmployerQuery("?"), "Работодатель в вакансии не назван", registry.snapshot_date)
        if _set(vacancy, record.status, record.note):
            storage.vacancies.set_facts(vacancy.id, vacancy.facts)
            changed.append(vacancy)
    summary.checked = len(changed)
    summary.moved = reevaluate(config, storage, changed, with_text=False) if changed else 0
    log.info("IT accreditation: %d vacancies updated, %d employers asked, %d moved", summary.checked,
             summary.employers_asked, summary.moved)
    return summary


def _set(vacancy: VacancyRecord, status: str, note: str) -> bool:
    facts = vacancy.facts
    assert facts is not None
    if facts.it_accreditation == status and facts.it_accreditation_note == note:
        return False
    facts.it_accreditation = status  # type: ignore[assignment]
    facts.it_accreditation_note = note
    return True
