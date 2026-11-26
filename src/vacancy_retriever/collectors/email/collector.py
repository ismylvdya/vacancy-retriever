from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from email.message import Message
from typing import Any, ClassVar

from ...config import AppConfig, MailboxConfig, SenderRule
from ...notices import add_once
from ..hh_descriptions import HhDescriptions, hh_description_url
from ..linkedin_guest import JOB_URL as LINKEDIN_JOB_URL
from ..linkedin_guest import GuestSettings, LinkedInGuestFetcher, create_egress
from ..job_search import JobSearchLookup, create_lookup
from ..base import CollectContext, Collector, CollectResult, RawItem, SourceError, SourceNotConfigured
from ..registry import register
from .generic import decoded_header, message_date, message_text, parse_generic
from .imap import ImapFactory, Mailbox, MailError
from .parsers import DETAIL_PARSERS, PARSERS, DetailResult, IgnoreLetter, UnknownLetter, load_parsers
from .triage import job_related, triage_item

log = logging.getLogger(__name__)

# TEMPORARY (2026-10-26): templates without a real sample yet. The first letter of each raises a UI notice asking
# the user to tell Claude, who then checks the parser on that letter, adds a masked sample and removes the entry.
# Templates written without a real sample: the first such letter leaves a notice (with its Message-ID) to check the
# parser against it. hh_saved_search checked on the letter of 28.10.2026 (samples/emails/hh_ru/yandex_502.eml).
WATCHED_TEMPLATES: dict[str, str] = {}


def archived(message: Message, senders: list[str]) -> bool:
    """The From header (only it) names an archived sender: «linkedin.com» also matches «@em.linkedin.com»."""
    sender = decoded_header(message, "From").lower()
    return any(entry.strip().lower() and entry.strip().lower() in sender for entry in senders)


def sender_rule(message: Message, senders: list[SenderRule]) -> SenderRule | None:
    sender = decoded_header(message, "From").lower()
    return next((rule for rule in senders if rule.match.lower() in sender), None)


@dataclass
class LetterItems:
    """What one letter gave: items for the run, or nothing because the template is unknown (letter untouched).
    triage: the letter is not a vacancy alert of a known template — the mail LLM stage says what it is."""

    items: list[RawItem]
    unknown: str | None = None
    triage: str | None = None  # why (the parser's reason), None = not for triage


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
        # No precise parser: the mail stage tells vacancies from statuses / waves first (decision 20.11.2026).
        return LetterItems([], triage="no precise parser")
    parser = PARSERS.get(rule.parser)
    if parser is None:
        return LetterItems([], unknown=f"no parser '{rule.parser}'")
    try:
        parsed = parser(message)
    except IgnoreLetter as exc:
        # A known letter without vacancies may still be a status of an application or an internship wave.
        return LetterItems([], triage=str(exc) or "Not a vacancy letter")
    except UnknownLetter as exc:
        return LetterItems([], triage=f"unknown template: {exc or subject}")
    except Exception as exc:  # noqa: BLE001 - a broken parser must not cost tokens: leave the letter for later
        log.warning("Parser %s failed on %s: %s", rule.parser, message_id, exc)
        return LetterItems([], unknown=f"parser error: {exc}")
    if not parsed:
        return LetterItems([], triage=f"unknown template: {subject}")
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
    """Unread letters in one IMAP mailbox: vacancy alerts of known senders, and (mail triage) any letter about work —
    statuses of applications, internship waves. Never changes the mailbox while collecting."""

    type_name = "email"
    imap_factory: ClassVar[ImapFactory | None] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._detail_cache: dict[str, DetailResult] = {}
        self._guest: LinkedInGuestFetcher | None = None
        self._search: JobSearchLookup | None = None
        self._hh_descriptions: HhDescriptions | None = None
        self._stop = threading.Event()

    def bind_stop(self, event: threading.Event) -> None:
        self._stop = event

    def _job_search(self) -> JobSearchLookup | None:
        """Google for Jobs lookup (no LinkedIn requests) — the main way to describe LinkedIn vacancies."""
        if self._search is None:
            self._search = create_lookup(AppConfig())
        return self._search

    def _linkedin(self) -> LinkedInGuestFetcher:
        if self._guest is None:
            config = AppConfig()
            pages = config.mail.linkedin_public_pages
            self._guest = LinkedInGuestFetcher(
                GuestSettings(pages.enabled, pages.max_per_day, pages.min_delay, pages.max_delay, pages.pause_days),
                config.paths.data / "linkedin_guest.json", egress=create_egress(config), stop_event=self._stop,
                still_enabled=lambda: AppConfig().mail.linkedin_public_pages.enabled)
        return self._guest

    def _guest_ready(self) -> bool:
        """The fallback is switched on and has an exit — then a vacancy Google did not find waits for its page."""
        pages = AppConfig().mail.linkedin_public_pages
        return pages.enabled and create_egress(AppConfig()) is not None

    def can_fetch_detail(self, url: str, allowed_domains: set[str]) -> bool:
        # LinkedIn: Google for Jobs here and now; LinkedIn's own page only later, for the misses (fetch_deferred).
        if LINKEDIN_JOB_URL.match(url):
            search = self._job_search()
            return search is not None and search.available()
        return super().can_fetch_detail(url, allowed_domains)

    def defer_detail(self, item: RawItem) -> date | None:
        if not LINKEDIN_JOB_URL.match(item.detail_url or "") or not self._guest_ready():
            return None
        return date.today() + timedelta(days=AppConfig().mail.linkedin_public_pages.delay_days)

    def fetch_deferred(self, item: RawItem, waited_days: int) -> tuple[str | None, bool]:
        """A vacancy Google for Jobs did not find: its public LinkedIn page, through the private exit only."""
        if not LINKEDIN_JOB_URL.match(item.detail_url or "") or not AppConfig().mail.linkedin_public_pages.enabled:
            return None, True  # switched off: nothing is opened, a waiting vacancy is evaluated from its letter at once
        guest = self._linkedin()
        gave_up = waited_days >= AppConfig().mail.linkedin_public_pages.max_wait_days
        if not guest.available():
            return None, gave_up
        text = guest.fetch(item.detail_url)
        # Nothing and LinkedIn still open = an honest «no page» (404, no description): final. A refusal or an exit
        # problem closes LinkedIn: the vacancy keeps waiting until max_wait_days.
        return text, bool(text) or guest.available() or gave_up

    def detail_alert(self) -> str | None:
        return "; ".join(filter(None, [x.alert for x in (self._search, self._guest) if x])) or None

    def detail_note(self) -> str | None:
        return "; ".join(filter(None, [x.note for x in (self._search, self._guest) if x])) or None

    def finish_details(self) -> None:
        for helper in (self._search, self._guest, self._hh_descriptions):
            if helper is not None:
                helper.close()

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

        triage = config.mail.triage
        checked = ctx.mail_checked() if triage.enabled else set()
        no_job: list[str] = []
        letters_for_llm = 0
        try:
            with mailbox:
                mailbox.select(readonly=True)
                # Senders are matched here, not by IMAP SEARCH FROM: Yandex does not find "hh.ru" in "noreply@hh.ru".
                for uid, header in mailbox.unseen_headers(oldest):
                    ctx.check_cancel()
                    if archived(header, config.mail.archived_senders):
                        continue  # in the archive (mail.yaml → archived_senders): not read, not touched
                    rule = sender_rule(header, config.mail.senders)
                    message_id = (header.get("Message-ID") or f"{mailbox.config.id}:{uid}").strip()
                    if rule is None and (not triage.enabled or message_id in checked
                                         or is_known(f"mail:{message_id}")):
                        continue
                    if rule is None and not job_related(decoded_header(header, "From"),
                                                        decoded_header(header, "Subject")):
                        # Only now the text is read (once: a letter without job words is remembered).
                        fetched = mailbox.fetch(uid)
                        if not job_related("", "", message_text(fetched.message)):
                            no_job.append(message_id)
                            continue
                    else:
                        fetched = mailbox.fetch(uid)
                    if mailbox.has_processed_label(fetched):
                        continue
                    if rule is None:
                        letter = LetterItems([], triage="unknown sender")
                    else:
                        letter = items_from_message(self.source.id, mailbox.config.id, uid, fetched.message, rule,
                                                    is_known)
                        if not ctx.vacancies_wanted and letter.items and not letter.triage:
                            continue  # a vacancy letter while this mailbox's groups are off: left unread
                    if letter.triage:
                        if not triage.enabled or is_known(f"mail:{message_id}"):
                            continue
                        if letters_for_llm >= triage.max_per_run:
                            continue  # stays unread, comes in the next run
                        letters_for_llm += 1
                        letter.items = [triage_item(self.source.id, mailbox.config.id, uid, fetched.message,
                                                    rule.name if rule else "", rule.parser if rule else "",
                                                    letter.triage)]
                    taken.update(item.external_id for item in letter.items)
                    if letter.unknown:
                        unknown.append(f"{rule.name if rule else '?'}: {letter.unknown}")
                    self._watch(config, mailbox.config, uid, fetched.message, letter.items)
                    items.extend(letter.items)
                    if ctx.limit and len(items) >= ctx.limit:
                        return CollectResult(items=items[: ctx.limit])
        except MailError as exc:
            raise SourceError(str(exc)) from exc
        finally:
            if no_job:
                ctx.remember_checked(no_job)
        notes = []
        if unknown:
            notes.append(f"{len(unknown)} unrecognised letters left unread ({'; '.join(unknown[:3])})")
            log.info("%s: unrecognised letters: %s", self.source.id, unknown)
        if letters_for_llm >= triage.max_per_run:
            notes.append(f"more than {triage.max_per_run} letters to sort: the rest in the next run")
        return CollectResult(items=items, note="; ".join(notes) or None)

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
        if (item.meta.get('email') or {}).get('parser') == 'hh_ru':
            if not item.detail_url or item.detail_url != hh_description_url(item.url):
                return None
            if self._hh_descriptions is None:
                self._hh_descriptions = HhDescriptions()
            return self._hh_descriptions.fetch(item.url, item.text)
        match = LINKEDIN_JOB_URL.match(item.detail_url or "")
        if match:
            search = self._job_search()
            if search is None:
                return None
            return search.lookup(item.title, item.meta.get("company") or "", item.meta.get("location") or "",
                                 match.group(1))
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
