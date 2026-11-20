"""Mail triage with the LLM: what an unread letter about work is (decision of the user 20.11.2026, docs/STATUSES.md).

- status   — about the user's own application: received, viewed, invitation, test, passed / failed, offer, rejection…
             → a row on the Statuses tab (never linked to a vacancy); the letter is marked read after the commit.
- wave     — an internship wave / open recruitment without one concrete vacancy → scored like a vacancy, shown in the
             feed with the «Internship wave» tag.
- vacancies — a letter with concrete vacancies (a sender without a precise parser) → scored as usual.
- other / not_job / spam — closed. When the LLM is not sure (confidence low) the letter is shown on the Statuses tab
  as «Unclear»: no status may be lost (user). A letter from an unknown sender that is not about work stays unread.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from ..config import AppConfig
from ..storage import Storage
from ..storage.items import StoredRawItem
from ..storage.statuses import NewStatus
from .llm_stage import LlmCaller

log = logging.getLogger(__name__)

KINDS = {"status", "wave", "vacancies", "other", "not_job", "spam"}
# Stage → (label, tone). Tones: positive (green), neutral (blue), negative (grey), unclear (orange).
STAGES: dict[str, tuple[str, str]] = {
    "received": ("Application received", "neutral"),
    "viewed": ("Viewed", "neutral"),
    "in_review": ("Under review", "neutral"),
    "invited": ("Invitation", "positive"),
    "test": ("Test task", "positive"),
    "test_passed": ("Test passed", "positive"),
    "next_stage": ("Next stage", "positive"),
    "offer": ("Offer", "positive"),
    "test_failed": ("Test failed", "negative"),
    "rejected": ("Rejected", "negative"),
    "closed": ("Vacancy closed", "negative"),
    "info": ("Update", "neutral"),
    "unclear": ("Unclear", "unclear"),
}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {"type": "object"}}},
    "required": ["results"],
}


@dataclass
class LetterVerdict:
    raw_id: int
    kind: str
    confidence: str
    stage: str = "unclear"
    company: str = ""
    position: str = ""
    summary: str = ""

    @property
    def reason(self) -> str:
        return {"status": "Status of an application", "other": "Job-related letter without vacancies",
                "not_job": "Not about work", "spam": "Spam"}.get(self.kind, self.kind)


def parse_verdicts(data: dict[str, Any], expected: set[int]) -> list[LetterVerdict]:
    verdicts = []
    for entry in data.get("results") or []:
        try:
            raw_id = int(entry.get("input_id"))
        except (TypeError, ValueError):
            continue
        if raw_id not in expected:
            continue
        kind = str(entry.get("kind") or "").lower()
        confidence = str(entry.get("confidence") or "low").lower()
        stage = str(entry.get("stage") or "unclear").lower()
        verdicts.append(LetterVerdict(
            raw_id, kind if kind in KINDS else "status", confidence if confidence in {"high", "medium", "low"} else "low",
            stage if stage in STAGES else "unclear", str(entry.get("company") or "")[:200],
            str(entry.get("position") or "")[:200], str(entry.get("summary_ru") or "")[:400],
        ))
    return verdicts


class LetterSorter:
    def __init__(self, config: AppConfig, caller: LlmCaller):
        self._config = config
        self._caller = caller
        self._system = config.prompt("mail")

    def batches(self, letters: list[StoredRawItem]) -> list[list[StoredRawItem]]:
        size = max(1, self._config.mail.triage.batch_size)
        return [letters[i:i + size] for i in range(0, len(letters), size)]

    def user_prompt(self, batch: list[StoredRawItem]) -> str:
        limit = self._config.llm.max_input_chars
        payload = []
        for item in batch:
            email = item.meta.get("email") or {}
            entry = {"input_id": item.id, "from": email.get("from") or email.get("sender") or "",
                     "subject": email.get("subject") or item.title, "date": (item.published_at.isoformat()[:10]
                                                                            if item.published_at else ""),
                     "text": item.text[:limit]}
            payload.append(entry)
        return json.dumps(payload, ensure_ascii=False)

    async def sort(self, batch: list[StoredRawItem]) -> list[LetterVerdict]:
        models = self._config.llm.stage_models
        response = await self._caller.call("mail", self._system, self.user_prompt(batch), models.mail or models.dedup,
                                           len(batch), SCHEMA)
        return parse_verdicts(response.data, {item.id for item in batch})


def apply_letter_verdict(storage: Storage, run_id: int, item: StoredRawItem, verdict: LetterVerdict) -> str:
    """Stores what the letter is. Returns "status" (a row on the Statuses tab), "score" (goes on to scoring) or
    "closed"."""
    email = item.meta.get("email") or {}

    def status(stage: str, unsure: bool) -> str:
        storage.statuses.insert(run_id, NewStatus(
            source_id=item.source_id, stage=stage, raw_item_id=item.id,
            message_id=str(email.get("message_id") or ""), received_at=item.published_at,
            sender=str(email.get("from") or email.get("sender") or ""), subject=str(email.get("subject") or item.title),
            company=verdict.company, position=verdict.position, summary=verdict.summary,
            confidence=verdict.confidence, unsure=unsure))
        storage.items.set_meta(item.id, "letter_kind", "status")
        return "status"

    if verdict.kind == "status":
        return status(verdict.stage, verdict.confidence != "high")
    if verdict.kind in {"wave", "vacancies"}:
        kind = "wave" if verdict.kind == "wave" else "vacancies"
        storage.items.set_meta(item.id, "letter_kind", kind)
        item.meta["letter_kind"] = kind
        return "score"
    if verdict.confidence == "low":  # «maybe spam, maybe a status»: the user must see it
        return status("unclear", True)
    storage.items.set_meta(item.id, "letter_kind", verdict.kind)
    # A letter of an unknown sender (not one of mail.yaml → senders) is not touched: stays unread. Known job senders'
    # letters without vacancies are marked read and labelled, as before the triage.
    if (email.get("parser") or "triage") == "triage":
        storage.items.set_meta(item.id, "leave_unread", True)
        item.meta["leave_unread"] = True
    return "closed"
