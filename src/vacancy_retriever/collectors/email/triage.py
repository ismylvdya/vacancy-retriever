"""Unread letters that are not vacancy alerts: statuses of the user's applications (rejection, invitation, test…),
internship waves, letters with vacancies from unknown senders (decision of the user 20.11.2026, docs/STATUSES.md).

Here only the cheap part, without the LLM: is the letter about work at all (job words in the sender, subject or
text)? Such a letter becomes one item for the LLM (pipeline/mail_stage.py), which says what it is. Letters without
job words are not touched (stay unread) and are remembered by Message-ID so their text is not fetched again.
"""

from __future__ import annotations

import re
from email.message import Message
from typing import Any

from ..base import RawItem
from .generic import decoded_header, message_date, parse_generic

MAX_TEXT = 6000

# Word starts (no \b at the end): «отклик» finds «откликнулись», «interview» finds «interviews». Wide on purpose — the
# LLM sorts out the rest; a status must never be lost (user, 20.11.2026).
JOB_WORDS = [
    # Russian
    "отклик", "резюме", "ваканси", "собеседован", "интервью", "тестов", "тест ", "задани", "стажир", "отбор",
    "кандидат", "приглаш", "оффер", "предложени о работе", "предложение о работе", "соискател", "рекрутер",
    "hr-", "hr ", "эйчар", "следующ этап", "следующий этап", "этап", "к сожалению", "решени", "рассмотр",
    "работодател", "должност", "позици", "карьер", "трудоустр", "найм", "набор", "заявк", "анкет",
    "assessment", "ассессмент", "кейс", "практик",
    # English
    "application", "applied", "interview", "candidate", "candidacy", "position", "recruit", "hiring", "hire",
    "job", "vacanc", "career", "internship", "intern ", "offer", "assessment", "test task", "coding challenge",
    "take-home", "unfortunately", "next step", "next round", "shortlist", "talent", "onboarding",
    # Serbian (latin)
    "prijav", "intervju", "kandidat", "posao", "oglas", "konkurs", "praksa", "zaposlen",
]
_PATTERN = re.compile("|".join(re.escape(w) for w in sorted(set(JOB_WORDS), key=len, reverse=True)), re.I)
# Job platforms: anything from them is about work (statuses come from hh.ru, Habr, LinkedIn…).
JOB_SENDERS = ("hh.ru", "headhunter", "habr", "superjob", "linkedin", "getmatch", "geekjob", "rabota", "zarplata",
               "trudvsem", "avito", "careers", "career", "jobs", "job", "hr@", "recruit", "talent", "hiring",
               "internship", "huntflow", "potok", "friend.work", "e-staff", "infostud", "workday", "greenhouse",
               "lever.co", "smartrecruiters")


def job_related(sender: str, subject: str, text: str = "") -> bool:
    sender_l = sender.lower()
    if any(word in sender_l for word in JOB_SENDERS):
        return True
    return bool(_PATTERN.search(subject) or (text and _PATTERN.search(text[:MAX_TEXT])))


def triage_item(source_id: str, mailbox_id: str, uid: str, message: Message, sender_name: str = "",
                parser: str = "", parser_note: str = "") -> RawItem:
    """One letter as one item for the mail LLM stage. Its id is the Message-ID: never collected twice."""
    message_id = (message.get("Message-ID") or f"{mailbox_id}:{uid}").strip()
    subject = decoded_header(message, "Subject")
    sender = decoded_header(message, "From")
    meta: dict[str, Any] = {
        "email": {"mailbox": mailbox_id, "uid": uid, "sender": sender_name or sender, "subject": subject,
                  "parser": parser or "triage", "from": sender, "message_id": message_id},
        "triage": True,
    }
    if parser_note:
        meta["parser_note"] = parser_note
    return RawItem(source_id, f"mail:{message_id}", "", parse_generic(message)[:MAX_TEXT], message_date(message),
                   title=subject, multi=True, meta=meta)
