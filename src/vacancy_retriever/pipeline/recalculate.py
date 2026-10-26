from __future__ import annotations

from dataclasses import dataclass

from ..config import AppConfig
from ..scoring.engine import ScoringEngine
from ..storage import Storage
from ..rates import RatesStore
from ..timeutil import local, utcnow


@dataclass
class RecalculateResult:
    total: int
    changed: int


def recalculate(config: AppConfig, storage: Storage) -> RecalculateResult:
    """Re-apply rules.yaml to stored facts without any LLM call.
    Cards the user already acted on keep their status; only group/stars/flags are refreshed."""
    engine = ScoringEngine(config.rules)
    rates = RatesStore(config.paths)
    now = utcnow()
    changed = 0
    records = storage.vacancies.with_facts()
    for record in records:
        if record.facts is None:
            continue
        collected = local(record.created_at).date() if record.created_at else None
        verdict = engine.evaluate(record.facts, record.published_at, now, rates.for_date(collected))
        automatic = record.user_status in {"new", "auto_skipped", "hidden"} and not storage.vacancies.user_set(record.id)
        if (verdict.decision, verdict.group, verdict.stars, verdict.skip_reason) != (
            record.decision, record.group_no, record.stars, record.skip_reason
        ):
            changed += 1
        storage.vacancies.apply_recalculated(record.id, verdict, change_status=automatic)
    return RecalculateResult(total=len(records), changed=changed)
