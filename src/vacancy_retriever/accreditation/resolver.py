"""Is the employer of a vacancy an accredited IT company? (decision of the user 20.11.2026, docs/ACCREDITATION.md)

The name in a vacancy rarely matches the legal name in the register («Сбер» vs «ПАО Сбербанк» / «ООО СберТех»), and
a brand often has several legal entities: the bank is not accredited, its IT company is, and the user can ask to be
hired by the IT one. So an employer is checked as a group of companies:

1. candidates are found by code: register records whose name starts like the brand (also in Cyrillic for a Latin
   brand), and — when DADATA_API_KEY is set — DaData's legal entities with that name;
2. the LLM (one call per batch of employers) says which real company this is, how sure it is, which candidates belong
   to the same group, and adds legal entities it knows (name + INN);
3. the CODE decides: an active register record among them → «yes»; the company is surely identified, its INNs are
   real (check digit) and none is active in the register → «no»; anything else → «unknown».

«unknown» never hides a vacancy: it stays in its group with an orange tag. Results are cached per employer name in
the database; on a new register snapshot «yes» / «no» are re-decided from the stored entities without the LLM.
"""

from __future__ import annotations

import json
import re
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..config import AppConfig
from ..storage.accreditation import AccreditationRecord, AccreditationRepository
from ..timeutil import utcnow
from .dadata import DaData
from .names import compact, inn_digits, normalize, short_name, to_cyrillic, valid_inn
from .registry import Registry, RegistryRecord

log = logging.getLogger(__name__)

YES, NO, UNKNOWN = "yes", "no", "unknown"
LABELS = {YES: "IT accredited", NO: "No IT accreditation", UNKNOWN: "IT accreditation unknown"}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {"type": "object"}}},
    "required": ["results"],
}

# Words that are not a brand: «Группа компаний», «Банк», «Холдинг»…
_GENERIC = {"группа", "компаний", "компания", "холдинг", "банк", "bank", "group", "company", "holding", "tech",
            "технологии", "лаборатория", "lab", "labs", "digital", "диджитал", "it", "ит", "studio", "студия",
            "россия", "russia", "the", "центр", "center", "solutions", "systems", "system", "software"}


def employer_key(name: str) -> str:
    return compact(name)


def known_employer(name: str | None) -> bool:
    value = (name or "").strip().lower()
    return bool(employer_key(value)) and value not in {"unknown", "n/a", "none", "не указан", "не указано", "—", "-"}


@dataclass
class EmployerQuery:
    employer: str
    hints: dict[str, str] = field(default_factory=dict)  # title, city, link, a bit of the vacancy text

    @property
    def key(self) -> str:
        return employer_key(self.employer)


def brand_prefixes(name: str) -> list[str]:
    """Words of the brand to look for in the register: «Ozon Tech» → [«ozon», «озон»]."""
    words = [w for w in normalize(name).split() if w not in _GENERIC and len(w) >= 3]
    prefixes: list[str] = []
    for word in words[:2]:
        variants = [word]
        if word.isascii():
            variants.append(to_cyrillic(word))
        for variant in variants:
            stem = variant[:max(4, len(variant) - 1)] if len(variant) > 5 else variant
            if stem not in prefixes:
                prefixes.append(stem)
    return prefixes


def brand_names(name: str) -> list[str]:
    """Whole brand names an entity may literally carry: «VK / Одноклассники» → [vk, вк, одноклассники]."""
    names: list[str] = []
    for part in re.split(r"[/(),;|]+", name):
        words = [w for w in normalize(part).split() if w not in _GENERIC]
        if not words:
            continue
        brand = " ".join(words)
        for variant in (brand, to_cyrillic(brand) if brand.isascii() else ""):
            if variant and variant not in names:
                names.append(variant)
    return names


def related(employer: str, entity: str) -> bool:
    """The entity's name carries the brand (Latin or Cyrillic, whole or a 4-letter stem): «ОТП Банк» ~ «АО ОТП БАНК»."""
    target = compact(entity)
    if not target:
        return False
    keys = brand_names(employer) + brand_prefixes(employer)
    return any(compact(k) and compact(k) in target for k in keys)


def candidates_for(registry: Registry | None, query: EmployerQuery, dadata: DaData | None,
                   limit: int = 15) -> list[dict[str, Any]]:
    """Register records and DaData entities that might be this employer, as refs the LLM can pick."""
    found: dict[str, dict[str, Any]] = {}
    if registry is not None:
        records = registry.find_name(query.employer)
        for brand in brand_names(query.employer):  # whole short brands too: «VK» → ООО «ВК»
            records += sorted(registry.find_name(brand), key=lambda r: not r.active)[:3]
        for prefix in brand_prefixes(query.employer):
            records += registry.with_prefix(prefix, limit)
        for record in records:
            if record.inn not in found and len(found) < limit:
                found[record.inn] = {"name": short_name(record.name), "inn": record.inn, "source": "register",
                                     "register": "active" if record.active else "annulled"}
    if dadata is not None:
        for party in dadata.suggest(query.employer):
            if party.inn in found:
                continue
            record = registry.find_inn(party.inn) if registry else None
            found[party.inn] = {"name": party.name, "inn": party.inn, "source": "dadata", "okved": party.okved,
                                "city": party.city,
                                "register": ("active" if record.active else "annulled") if record else "absent"}
    return [{"ref": f"c{i}", **c} for i, c in enumerate(found.values(), 1)]


def _lookup(registry: Registry, entity: dict[str, Any]) -> RegistryRecord | None:
    inn = inn_digits(entity.get("inn"))
    if inn:
        record = registry.find_inn(inn)
        if record is not None:
            return record
    by_name = registry.find_name(str(entity.get("name") or ""))
    return next((r for r in by_name if r.active), by_name[0] if by_name else None)


def decide(details: dict[str, Any], registry: Registry) -> tuple[str, str]:
    """The status and its note from what is known about the employer (no LLM, no network)."""
    entities = [e for e in details.get("entities") or [] if isinstance(e, dict)]
    found = [(e, r) for e in entities if (r := _lookup(registry, e)) is not None]
    active = [r for _, r in found if r.active]
    if active:
        return YES, "; ".join(dict.fromkeys(r.label() for r in active[:3]))
    note = str(details.get("note") or "")
    if details.get("russian") is False:
        return UNKNOWN, note or "Похоже, не российская компания"
    # «No» only when the LLM is sure it named the whole group (no other entity hiring IT staff may exist): an annulled
    # bank or an unlisted IT subsidiary is not a «no» (VK: «Одноклассники» annulled, ООО «ВК» active).
    if details.get("identified") == "high" and details.get("russian") is True and details.get("group_complete") is True:
        annulled = [r for _, r in found if not r.active]
        if annulled:
            return NO, "; ".join(dict.fromkeys(r.label() for r in annulled[:3]))
        real = [e for e in entities if valid_inn(e.get("inn"))]
        checked = [e for e in real if e.get("source") == "dadata"] or real
        if checked:
            names = "; ".join(f"{e.get('name') or '?'} (ИНН {inn_digits(e.get('inn'))})" for e in checked[:3])
            return NO, f"{names} — нет в реестре"
    return UNKNOWN, note or "Не удалось точно определить юрлицо компании"


class AccreditationResolver:
    def __init__(self, config: AppConfig, repository: AccreditationRepository, registry: Registry | None,
                 ask_llm: Callable[[str, str, int], Awaitable[dict[str, Any]]] | None,
                 dadata: DaData | None = None, now: Callable[[], datetime] = utcnow):
        self._config = config
        self._repository = repository
        self._registry = registry
        self._ask_llm = ask_llm  # (system, user, n_items) → parsed JSON reply
        self._dadata = dadata
        self._now = now

    @property
    def registry_date(self) -> str:
        return self._registry.snapshot_date if self._registry else ""

    def cached(self, keys: list[str]) -> dict[str, AccreditationRecord]:
        """Cached results, re-decided by code (current rules, current snapshot) from the stored entities."""
        records = self._repository.get_many(keys)
        if self._registry is None:
            return records
        for record in records.values():
            if not record.details:  # cheap, no network
                continue
            status, note = decide(record.details, self._registry)
            if status == NO:
                status, note = self._guard_no(record.employer, status, note, record.details)
            if (status, note, record.registry_date) != (record.status, record.note, self._registry.snapshot_date):
                record.status, record.note = status, note
                record.registry_date = self._registry.snapshot_date
                self._repository.save(record)
        return records

    def needs_llm(self, record: AccreditationRecord | None, recheck_unknown: bool = False) -> bool:
        if record is None:
            return True
        if record.status != UNKNOWN or not record.checked_at:
            return False
        if recheck_unknown:
            return True
        return self._now() - record.checked_at >= timedelta(days=self._config.accreditation.recheck_unknown_days)

    def batches(self, queries: list[EmployerQuery]) -> list[list[EmployerQuery]]:
        size = max(1, self._config.accreditation.batch_size)
        return [queries[i:i + size] for i in range(0, len(queries), size)]

    async def resolve(self, batch: list[EmployerQuery]) -> list[AccreditationRecord]:
        """One LLM call for a batch of employers; every employer of the batch gets a cached record."""
        if self._registry is None or self._ask_llm is None:
            return []
        inputs = []
        candidates: dict[int, list[dict[str, Any]]] = {}
        for index, query in enumerate(batch, 1):
            candidates[index] = candidates_for(self._registry, query, self._dadata)
            inputs.append({"input_id": index, "employer": query.employer, "vacancy": query.hints,
                           "candidates": [{k: v for k, v in c.items() if k != "source"} for c in candidates[index]]})
        reply = await self._ask_llm(self._config.prompt("accreditation"), json.dumps(inputs, ensure_ascii=False),
                                    len(batch))
        answers = {}
        for entry in reply.get("results") or []:
            try:
                answers[int(entry.get("input_id"))] = entry
            except (TypeError, ValueError):
                continue
        records = []
        for index, query in enumerate(batch, 1):
            answer = answers.get(index)
            if answer is None:
                continue  # not in the reply: asked again next run
            details = self._details(answer, candidates[index])
            self._complete_entities(details)
            status, note = decide(details, self._registry)
            if status == NO:
                status, note = self._guard_no(query.employer, status, note, details)
            record = AccreditationRecord(query.key, query.employer, status, note, details,
                                         self._registry.snapshot_date, self._now())
            self._repository.save(record)
            records.append(record)
        return records

    def _guard_no(self, employer: str, status: str, note: str, details: dict[str, Any]) -> tuple[str, str]:
        """A «no» from the LLM's list alone is not trusted:
        - none of the entities looks like the brand (Haiku once gave Wink the INN of «СК Росгосстрах Жизнь») → unknown;
        - an ACTIVE entity literally named like the brand (register, then DaData) may be the group's IT company the
          LLM forgot (VK → ООО «ВК» 7743001840) → unknown."""
        assert self._registry is not None
        if not any(related(employer, str(e.get("name") or "")) for e in details.get("entities") or []
                   if isinstance(e, dict)):
            return UNKNOWN, f"Найденное юрлицо не похоже на «{employer}» — не проверено ({note})"[:300]
        for brand in brand_names(employer):
            records = [r for r in self._registry.find_name(brand) if r.active]
            if not records and self._dadata is not None:
                records = [r for p in self._dadata.suggest(brand, count=10)
                           if compact(p.name) == compact(brand) and (r := self._registry.find_inn(p.inn)) and r.active]
            if records:
                return UNKNOWN, (f"{records[0].label()}: одноимённое юрлицо действует в реестре — возможно, компания "
                                 f"группы ({note})")[:300]
        return status, note

    def _complete_entities(self, details: dict[str, Any]) -> None:
        """Entities the LLM named without an INN (it never invents one): the register by exact name, then DaData by
        name — «ООО Икс 5 Диджитал» gets its INN and can be checked."""
        assert self._registry is not None
        known = {e.get("inn") for e in details["entities"] if e.get("inn")}
        for entity in list(details["entities"]):
            if entity.get("inn") or not entity.get("name"):
                continue
            hits = self._registry.find_name(entity["name"])
            if hits:
                best = next((r for r in hits if r.active), hits[0])
                entity["inn"], entity["source"] = best.inn, "register_name"
                continue
            if self._dadata is None:
                continue
            target = compact(entity["name"])
            for party in self._dadata.suggest(entity["name"], count=3):
                if party.inn in known:
                    break
                if compact(party.name) == target or target in compact(party.name) or compact(party.name) in target:
                    entity["inn"], entity["source"] = party.inn, "dadata"
                    known.add(party.inn)
                    break

    @staticmethod
    def _details(answer: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
        by_ref = {c["ref"]: c for c in candidates}
        entities: list[dict[str, Any]] = []
        for ref in answer.get("matching_candidates") or []:
            candidate = by_ref.get(str(ref))
            if candidate:
                entities.append({"name": candidate["name"], "inn": candidate["inn"], "source": candidate["source"]})
        for entity in answer.get("legal_entities") or []:
            if isinstance(entity, dict) and (entity.get("name") or entity.get("inn")):
                inn = inn_digits(entity.get("inn"))
                if any(e["inn"] == inn for e in entities if inn):
                    continue
                entities.append({"name": str(entity.get("name") or ""), "inn": inn, "source": "llm"})
        identified = str(answer.get("identified") or "low").lower()
        russian = answer.get("russian_company")
        complete = answer.get("other_it_entities_possible")
        return {
            "identified": identified if identified in {"high", "medium", "low"} else "low",
            "russian": russian if isinstance(russian, bool) else None,
            "group_complete": complete is False,
            "entities": entities,
            "note": str(answer.get("note") or "")[:300],
        }


def unknown_record(query: EmployerQuery, note: str, registry_date: str = "") -> AccreditationRecord:
    return AccreditationRecord(query.key, query.employer, UNKNOWN, note, {}, registry_date, utcnow())
