"""`vacancy-retriever export-email-samples`: copy a few letters per known sender into samples/emails/.
Read-only: messages are fetched with BODY.PEEK and the mailbox is opened with EXAMINE (nothing is marked read)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage, Message
from pathlib import Path

from ...config import AppConfig
from .collector import sender_rule
from .generic import _part_text, decoded_header
from .imap import Mailbox, MailError


# Long parameter values are tokens / tracking ids — except redirect targets (u=https%3A…), which parsers need.
_LONG_PARAM = re.compile(r"(?<=[?&;])([A-Za-z_][\w.\-]*=)(?!https?(?:%3A|:))[^&\s\"'<>]{16,}", re.I)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_EMAIL = re.compile(r"[\w.+-]+(?:@|%40)[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d\s()\-]{9,}\d")


def mask_text(text: str, words: list[str]) -> str:
    """Personal data out of a sample: e-mail addresses, phone numbers, the user's names, login / tracking tokens
    (any query parameter value of 16+ characters) and UUIDs. Shapes of links and the layout stay intact."""
    text = _LONG_PARAM.sub(lambda m: m.group(1) + "MASKED", text)
    text = _UUID.sub("00000000-0000-0000-0000-000000000000", text)
    text = _EMAIL.sub("hidden@example.com", text)
    text = _PHONE.sub("+70000000000", text)
    for word in sorted({w for w in words if len(w) >= 3}, key=len, reverse=True):
        text = re.sub(re.escape(word), "Ivan" if word[:1].isupper() else "ivan", text, flags=re.I)
    return text


def mask_message(message: Message, words: list[str], index: int) -> EmailMessage:
    """A fresh letter with the sender, subject, date and masked bodies — nothing else from the original."""
    masked = EmailMessage()
    masked["From"] = message.get("From", "")
    masked["To"] = "hidden@example.com"
    masked["Subject"] = mask_text(decoded_header(message, "Subject"), words)
    masked["Date"] = message.get("Date", "")
    masked["Message-ID"] = f"<sample-{index}@example.com>"
    if message.get("List-Unsubscribe"):
        masked["List-Unsubscribe"] = "<mailto:hidden@example.com>"
    plain, html = _part_text(message, "plain"), _part_text(message, "html")
    masked.set_content(mask_text(plain or "", words))
    if html:
        masked.add_alternative(mask_text(html, words), subtype="html")
    return masked


def mask_words(config: AppConfig) -> list[str]:
    """Logins from config/.env (and their local parts) + SAMPLE_MASK_WORDS (comma-separated names / phone)."""
    words: list[str] = []
    for mailbox in config.mail.mailboxes:
        login = config.secrets.get(mailbox.login_env) or ""
        words += [login, login.split("@")[0]] if login else []
    words += [w.strip() for w in (config.secrets.get("SAMPLE_MASK_WORDS") or "").split(",")]
    return [w for w in words if w]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "sender"


def export_samples(config: AppConfig, mailbox_id: str | None, limit: int, target: Path, raw: bool = False) -> int:
    """limit = letters per sender (0 = all). Masked by default (see mask_text); raw=True keeps the original."""
    since = datetime.now(UTC) - timedelta(days=config.mail.max_age_days)
    words = mask_words(config)
    exported = 0
    for mailbox_config in config.mail.mailboxes:
        if mailbox_id and mailbox_config.id != mailbox_id:
            continue
        login = config.secrets.get(mailbox_config.login_env)
        password = config.secrets.get(mailbox_config.password_env)
        if not login or not password:
            print(f"{mailbox_config.id}: not configured ({mailbox_config.login_env} / {mailbox_config.password_env})")
            continue
        try:
            with Mailbox(mailbox_config, login, password, config.mail.processed_label) as mailbox:
                mailbox.select(readonly=True)
                taken: dict[str, int] = {}
                for uid, header in mailbox.unseen_headers(since):
                    rule = sender_rule(header, config.mail.senders)
                    if rule is None or (limit and taken.get(rule.name, 0) >= limit):
                        continue
                    taken[rule.name] = taken.get(rule.name, 0) + 1
                    fetched = mailbox.fetch(uid)
                    folder = target / _slug(rule.name)
                    folder.mkdir(parents=True, exist_ok=True)
                    path = folder / f"{mailbox_config.id}_{uid}.eml"
                    letter = fetched.message if raw else mask_message(fetched.message, words, exported)
                    path.write_bytes(letter.as_bytes())
                    exported += 1
                    print(f"saved {path.relative_to(target.parent.parent)}")
        except MailError as exc:
            print(f"{mailbox_config.id}: {exc}")
    note = "NOT masked" if raw else "masked (names from SAMPLE_MASK_WORDS in config/.env)"
    print(f"Exported {exported} e-mails, {note}. Still check them before committing to git.")
    return 0
