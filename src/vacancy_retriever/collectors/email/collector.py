from __future__ import annotations

import logging
from collections import defaultdict
from datetime import timedelta
from email.message import Message
from typing import Any, ClassVar

from ...config import AppConfig, MailboxConfig, SenderRule
from ..base import CollectContext, Collector, CollectResult, RawItem, SourceError, SourceNotConfigured
from ..registry import register
from .generic import decoded_header, message_date, parse_generic
from .imap import ImapFactory, Mailbox, MailError
from .parsers import PARSERS, load_parsers

log = logging.getLogger(__name__)


def sender_rule(message: Message, senders: list[SenderRule]) -> SenderRule | None:
    sender = decoded_header(message, "From").lower()
    return next((rule for rule in senders if rule.match.lower() in sender), None)


def items_from_message(source_id: str, mailbox_id: str, uid: str, message: Message,
                       rule: SenderRule) -> list[RawItem]:
    message_id = (message.get("Message-ID") or f"{mailbox_id}:{uid}").strip()
    published = message_date(message)
    subject = decoded_header(message, "Subject")
    meta: dict[str, Any] = {"email": {"mailbox": mailbox_id, "uid": uid, "sender": rule.name, "subject": subject}}
    parser = PARSERS.get(rule.parser) if rule.parser != "generic" else None
    if parser is not None:
        try:
            parsed = parser(message)
        except Exception as exc:  # noqa: BLE001 - fall back to the generic parser
            log.warning("Parser %s failed on %s: %s", rule.parser, message_id, exc)
            parsed = []
        if parsed:
            return [
                RawItem(source_id, f"{message_id}#{index}", vacancy.url, vacancy.text or vacancy.title, published,
                        title=vacancy.title, multi=False,
                        meta={**meta, "company": vacancy.company, "location": vacancy.location})
                for index, vacancy in enumerate(parsed)
            ]
    return [RawItem(source_id, message_id, "", parse_generic(message), published, title=subject, multi=True,
                    meta=meta)]


@register
class EmailCollector(Collector):
    """Unread letters from known senders in one IMAP mailbox. Never changes the mailbox while collecting."""

    type_name = "email"
    imap_factory: ClassVar[ImapFactory | None] = None

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
        seen: set[str] = set()
        try:
            with mailbox:
                mailbox.select(readonly=True)
                for rule in config.mail.senders:
                    ctx.check_cancel()
                    for uid in mailbox.search_from(rule.match, oldest):
                        ctx.check_cancel()
                        if uid in seen:
                            continue
                        seen.add(uid)
                        fetched = mailbox.fetch(uid)
                        if mailbox.has_processed_label(fetched):
                            continue
                        exact = sender_rule(fetched.message, config.mail.senders) or rule
                        new = items_from_message(self.source.id, mailbox.config.id, uid, fetched.message, exact)
                        items.extend(i for i in new if not ctx.is_known(i.external_id))
                        if ctx.limit and len(items) >= ctx.limit:
                            return CollectResult(items=items[: ctx.limit])
        except MailError as exc:
            raise SourceError(str(exc)) from exc
        return CollectResult(items=items)

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
