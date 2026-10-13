"""`vacancy-retriever export-email-samples`: copy a few letters per known sender into samples/emails/.
Read-only: messages are fetched with BODY.PEEK and the mailbox is opened with EXAMINE (nothing is marked read)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ...config import AppConfig
from .imap import Mailbox, MailError


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "sender"


def export_samples(config: AppConfig, mailbox_id: str | None, limit: int, target: Path) -> int:
    since = datetime.now(UTC) - timedelta(days=config.mail.max_age_days)
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
                for rule in config.mail.senders:
                    uids = mailbox.search_from(rule.match, since)[-limit:]
                    folder = target / _slug(rule.name)
                    for uid in uids:
                        fetched = mailbox.fetch(uid)
                        folder.mkdir(parents=True, exist_ok=True)
                        path = folder / f"{mailbox_config.id}_{uid}.eml"
                        path.write_bytes(fetched.message.as_bytes())
                        exported += 1
                        print(f"saved {path.relative_to(target.parent.parent)}")
        except MailError as exc:
            print(f"{mailbox_config.id}: {exc}")
    print(f"Exported {exported} e-mails. They may contain personal data — check before committing to git.")
    return 0
