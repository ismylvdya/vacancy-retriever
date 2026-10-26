from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from email.message import Message
from typing import Any, ClassVar

from ...config import AppConfig, MailboxConfig, SenderRule
from ...notices import add_once
from ..base import CollectContext, Collector, CollectResult, RawItem, SourceError, SourceNotConfigured
from ..registry import register
from .generic import decoded_header, message_date, parse_generic
from .imap import ImapFactory, Mailbox, MailError
from .parsers import DETAIL_PARSERS, PARSERS, DetailResult, IgnoreLetter, UnknownLetter, load_parsers

log = logging.getLogger(__name__)

# TEMPORARY (2026-10-26): templates without a real sample yet. The first letter of each raises a UI notice asking
# the user to tell Claude, who then checks the parser on that letter, adds a masked sample and removes the entry.
WATCHED_TEMPLATES = {"hh_saved_search": "первое письмо автопоиска hh.ru"}


def sender_rule(message: Message, senders: list[SenderRule]) -> SenderRule | None:
    sender = decoded_header(message, "From").lower()
    return next((rule for rule in senders if rule.match.lower() in sender), None)


@dataclass
class LetterItems:
    """What one letter gave: items for the run, or nothing because the template is unknown (letter untouched)."""

    items: list[RawItem]
    unknown: str | None = None


def items_from_message(source_id: str, mailbox_id: str, uid: str, message: Message, rule: SenderRule,
                       is_known: Callable[[str], bool] = lambda _id: False) -> LetterItems:
    message_id = (message.get("Message-ID") or f"{mailbox_id}:{uid}").strip()
    published = message_date(message)
    subject = decoded_header(message, "Subject")
    meta: dict[str, Any] = {"email": {"mailbox": mailbox_id, "uid": uid, "sender": rule.name, "subject": subject,
                                      "parser": rule.parser}}

    def ignored(reason: str) -> LetterItems:
        # One hidden item per letter: no LLM, shown in Processed → Hidden, the letter is marked after the commit.
        # Message-ID in the text: letters with the same subject must not look like reposts of each other.
        item = RawItem(source_id, message_id, "", f"{subject}\n{message_id}", published, title=subject,
                       meta={**meta, "ignore": reason})
        return LetterItems([] if is_known(item.external_id) else [item])

    if rule.parser == "generic":
        return LetterItems([RawItem(source_id, message_id, "", parse_generic(message), published, title=subject,
                                    multi=True, meta=meta)])
    parser = PARSERS.get(rule.parser)
    if parser is None:
        return LetterItems([], unknown=f"no parser '{rule.parser}'")
    try:
        parsed = parser(message)
    except IgnoreLetter as exc:
        return ignored(str(exc) or "Not a vacancy letter")
    except UnknownLetter as exc:
        return LetterItems([], unknown=str(exc) or subject)
    except Exception as exc:  # noqa: BLE001 - a broken parser must not cost tokens: leave the letter for later
        log.warning("Parser %s failed on %s: %s", rule.parser, message_id, exc)
        return LetterItems([], unknown=f"parser error: {exc}")
    if not parsed:
        return LetterItems([], unknown=subject)
    items = []
    for index, vacancy in enumerate(parsed):
        item = RawItem(source_id, vacancy.key or f"{message_id}#{index}", vacancy.url, vacancy.text or vacancy.title,
                       published, title=vacancy.title, multi=False, detail_url=vacancy.detail_url or None,
                       meta={**meta, "company": vacancy.company, "location": vacancy.location, **vacancy.extra})
        if not is_known(item.external_id):
            items.append(item)
    return LetterItems(items) if items else ignored("All vacancies already collected from earlier letters")


@register
class EmailCollector(Collector):
    """Unread letters from known senders in one IMAP mailbox. Never changes the mailbox while collecting."""

    type_name = "email"
    imap_factory: ClassVar[ImapFactory | None] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._detail_cache: dict[str, DetailResult] = {}

    def _settings(self) -> tuple[AppConfig, MailboxConfig, str, str]:
        config = AppConfig()
        mailbox_id = self.source.params.get("mailbox", self.source.id)
        mailbox = next((m for m in config.mail.mailboxes if m.id == mailbox_id), None)
        if mailbox is None:
            raise SourceError(f"Mailbox '{mailbox_id}' is not described in config/mail.yaml")
        login = config.secrets.get(mailbox.login_env)
        password = config.secrets.get(mailbox.password_env)
        if not login or not password:
            raise SourceNotConfigured(f"set {mailbox.login_env} and {mailbox.password_env} in config/.env")
        return config, mailbox, login, password

    def _mailbox(self) -> tuple[AppConfig, Mailbox]:
        config, mailbox, login, password = self._settings()
        kwargs = {"factory": self.imap_factory} if self.imap_factory else {}
        return config, Mailbox(mailbox, login, password, config.mail.processed_label, **kwargs)

    def collect(self, ctx: CollectContext) -> CollectResult:
        config, mailbox = self._mailbox()
        load_parsers()
        since = max(ctx.since, ctx.since.replace(microsecond=0))
        oldest = since - timedelta(days=1)
        items: list[RawItem] = []
        unknown: list[str] = []
        # Vacancies taken from earlier letters of this same pass are "known" too: the database only learns about them
        # after the collector returns, and a letter repeating them must become "all vacancies already collected".
        taken: set[str] = set()

        def is_known(external_id: str) -> bool:
            return external_id in taken or ctx.is_known(external_id)

        try:
            with mailbox:
                mailbox.select(readonly=True)
                # Senders are matched here, not by IMAP SEARCH FROM: Yandex does not find "hh.ru" in "noreply@hh.ru".
                for uid, header in mailbox.unseen_headers(oldest):
                    ctx.check_cancel()
                    rule = sender_rule(header, config.mail.senders)
                    if rule is None:
                        continue
                    fetched = mailbox.fetch(uid)
                    if mailbox.has_processed_label(fetched):
                        continue
                    letter = items_from_message(self.source.id, mailbox.config.id, uid, fetched.message, rule,
                                                is_known)
                    taken.update(item.external_id for item in letter.items)
                    if letter.unknown:
                        unknown.append(f"{rule.name}: {letter.unknown}")
                    self._watch(config, mailbox.config, uid, fetched.message, letter.items)
                    items.extend(letter.items)
                    if ctx.limit and len(items) >= ctx.limit:
                        return CollectResult(items=items[: ctx.limit])
        except MailError as exc:
            raise SourceError(str(exc)) from exc
        note = f"{len(unknown)} unrecognised letters left unread ({'; '.join(unknown[:3])})" if unknown else None
        if unknown:
            log.info("%s: unrecognised letters: %s", self.source.id, unknown)
        return CollectResult(items=items, note=note)

    def sender_icons(self) -> list[tuple[str, str]]:
        """(icon id, site) of senders whose favicon is downloaded from their site (not linkedin.com — forbidden)."""
        config = AppConfig()
        return sorted({(rule.icon_id, rule.site) for rule in config.mail.senders if rule.icon_id and rule.site})

    @staticmethod
    def _watch(config: AppConfig, mailbox: MailboxConfig, uid: str, message: Message, items: list[RawItem]) -> None:
        for template in {item.meta.get("template") for item in items} & WATCHED_TEMPLATES.keys():
            subject = decoded_header(message, "Subject")
            text = (
                f"Vacancy Retriever встретил {WATCHED_TEMPLATES[template]} (шаблон {template}).\n"
                f"Ящик: {mailbox.name} ({mailbox.id}), папка {mailbox.folder}; после успешного прогона письмо помечено "
                f"прочитанным и лежит в «{config.mail.processed_label}».\n"
                f"Тема: «{subject}»\n"
                f"Дата: {message.get('Date', '')}\n"
                f"Message-ID: {(message.get('Message-ID') or '').strip()} (IMAP UID {uid})\n"
                f"Вакансий в письме по парсеру: {len([i for i in items if not i.meta.get('ignore')])}.\n"
                "Найди это письмо по Message-ID, сверь парсер с настоящим шаблоном, добавь замаскированный образец "
                "в samples/emails/ и expected.json, убери шаблон из WATCHED_TEMPLATES (collectors/email/collector.py)."
            )
            if add_once(config.paths.notices, f"first:{template}", f"First letter: {WATCHED_TEMPLATES[template]}", text):
                log.info("Notice: first %s letter (%s)", template, subject)

    def fetch_detail(self, item: RawItem) -> str | None:
        result = self._detail(item)
        return result.text if result else None

    def detail_skip_reason(self, item: RawItem, text: str) -> str | None:
        result = self._detail_cache.get(item.detail_url or "")
        return result.skip_reason if result else None

    def _detail(self, item: RawItem) -> DetailResult | None:
        load_parsers()
        parser = DETAIL_PARSERS.get(((item.meta.get("email") or {}).get("parser")) or "")
        if parser is None or not item.detail_url:
            return None
        result = parser(self.http.get_text(item.detail_url))
        if result is not None:
            self._detail_cache[item.detail_url] = result
        return result

    def after_commit(self, processed_meta: list[dict[str, Any]]) -> list[str]:
        uids: dict[str, set[str]] = defaultdict(set)
        for meta in processed_meta:
            email_meta = meta.get("email") or {}
            if email_meta.get("uid"):
                uids[email_meta.get("mailbox", "")].add(str(email_meta["uid"]))
        if not uids:
            return []
        try:
            _, mailbox = self._mailbox()
            with mailbox:
                mailbox.mark_processed(sorted(uids.get(mailbox.config.id, set()), key=int))
        except (MailError, SourceNotConfigured, SourceError) as exc:
            return [str(exc)]
        return []
