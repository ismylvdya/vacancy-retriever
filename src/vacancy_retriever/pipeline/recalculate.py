from __future__ import annotations

from dataclasses import dataclass

from ..config import AppConfig
from ..scoring.engine import ScoringEngine
from ..scoring.text_facts import TextFacts
from ..storage import Storage
from ..storage.vacancies import VacancyRecord
from ..rates import RatesStore
from ..timeutil import local, utcnow


@dataclass
class RecalculateResult:
    total: int
    changed: int


def recalculate(config: AppConfig, storage: Storage) -> RecalculateResult:
    """Re-apply rules.yaml to stored facts without any LLM call.
    Cards the user already acted on keep their status; only group/stars/flags are refreshed."""
    records = storage.vacancies.with_facts()
    return RecalculateResult(total=len(records), changed=reevaluate(config, storage, records))


def reevaluate(config: AppConfig, storage: Storage, records: list[VacancyRecord], with_text: bool = True) -> int:
    """The verdict of these committed vacancies from their stored facts (also after accreditation/stage.py changed
    them). Returns how many changed."""
    engine = ScoringEngine(config.rules)
    text_facts = TextFacts(config.rules)
    rates = RatesStore(config.paths)
    now = utcnow()
    changed = 0
    email_only = storage.items.email_only_ids()
    for record in records:
        if record.facts is None:
            continue
        raw = storage.items.get(record.primary_raw_id) if with_text else None
        if raw is not None:
            before = record.facts.model_dump_json()
            text_facts.fill(record.facts, raw)
            if record.facts.model_dump_json() != before:
                storage.vacancies.set_facts(record.id, record.facts)
        collected = local(record.created_at).date() if record.created_at else None
        verdict = engine.evaluate(record.facts, record.published_at, now, rates.for_date(collected),
                                  record.primary_raw_id in email_only)
        automatic = record.user_status in {"new", "auto_skipped", "hidden"} and not storage.vacancies.user_set(record.id)
        if (verdict.decision, verdict.group, verdict.stars, verdict.skip_reason) != (
            record.decision, record.group_no, record.stars, record.skip_reason
        ):
            changed += 1
        storage.vacancies.apply_recalculated(record.id, verdict, change_status=automatic)
    return changed
