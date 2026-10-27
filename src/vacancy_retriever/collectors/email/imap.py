from __future__ import annotations

import imaplib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from email import message_from_bytes
from email.message import Message
from email.policy import default as default_policy

from ... import traffic
from ...config import MailboxConfig
from . import utf7

log = logging.getLogger(__name__)

ImapFactory = Callable[[str, int], imaplib.IMAP4]
_UID = re.compile(rb"UID (\d+)")
_LABELS = re.compile(rb"X-GM-LABELS \((.*?)\)")


class MailError(Exception):
    pass


@dataclass
class FetchedMessage:
    uid: str
    message: Message
    labels: str


class Mailbox:
    """Read-only IMAP access (BODY.PEEK) plus post-commit labelling / moving."""

    def __init__(self, config: MailboxConfig, login: str, password: str, label: str,
                 factory: ImapFactory = imaplib.IMAP4_SSL):
        self.config = config
        self._login = login
        self._password = password
        self._label = label
        self._factory = factory
        self._imap: imaplib.IMAP4 | None = None

    def __enter__(self) -> Mailbox:
        try:
            self._imap = self._factory(self.config.host, self.config.port)
            self._imap.login(self._login, self._password)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise MailError(f"{self.config.name}: IMAP login failed: {exc}") from exc
        return self

    def __exit__(self, *exc: object) -> None:
        if self._imap is not None:
            try:
                self._imap.logout()
            except (imaplib.IMAP4.error, OSError):
                pass

    @property
    def imap(self) -> imaplib.IMAP4:
        assert self._imap is not None, "use Mailbox as a context manager"
        return self._imap

    def _check(self, response: tuple[str, list], action: str) -> list:
        status, data = response
        traffic.add(sum(sum(len(x) for x in part if isinstance(x, bytes)) if isinstance(part, tuple)
                        else len(part) if isinstance(part, bytes) else 0 for part in data or []))
        if status != "OK":
            raise MailError(f"{self.config.name}: {action} failed: {data!r}")
        return data

    def select(self, readonly: bool) -> None:
        self._check(self.imap.select(utf7.quoted(self.config.folder), readonly=readonly), "SELECT")

    def search_from(self, sender: str, since: datetime) -> list[str]:
        criteria = f'(UNSEEN SINCE {since.strftime("%d-%b-%Y")} FROM "{sender}")'
        data = self._check(self.imap.uid("SEARCH", None, criteria), "SEARCH")
        return [uid.decode() for uid in (data[0] or b"").split()]

    def unseen_headers(self, since: datetime, batch: int = 200) -> list[tuple[str, Message]]:
        """UNSEEN letters since the date with only their From / Subject / Date headers (BODY.PEEK)."""
        data = self._check(self.imap.uid("SEARCH", None, f'(UNSEEN SINCE {since.strftime("%d-%b-%Y")})'), "SEARCH")
        uids = [uid.decode() for uid in (data[0] or b"").split()]
        result: list[tuple[str, Message]] = []
        for start in range(0, len(uids), batch):
            chunk = ",".join(uids[start:start + batch])
            rows = self._check(self.imap.uid("FETCH", chunk, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"),
                               "FETCH headers")
            for part in rows:
                if isinstance(part, tuple):
                    uid = _UID.search(part[0])
                    if uid:
                        result.append((uid.group(1).decode(), message_from_bytes(part[1], policy=default_policy)))
        return result

    def fetch(self, uid: str) -> FetchedMessage:
        items = "(BODY.PEEK[] X-GM-LABELS)" if self.config.kind == "gmail" else "(BODY.PEEK[])"
        data = self._check(self.imap.uid("FETCH", uid, items), "FETCH")
        raw, header = b"", b""
        for part in data:
            if isinstance(part, tuple):
                header, raw = part[0], part[1]
                break
        labels_match = _LABELS.search(header)
        labels = labels_match.group(1).decode("utf-8", "replace") if labels_match else ""
        return FetchedMessage(uid, message_from_bytes(raw, policy=default_policy), labels)

    def has_processed_label(self, fetched: FetchedMessage) -> bool:
        if self.config.kind != "gmail":
            return False
        return self._label in fetched.labels or utf7.encode(self._label) in fetched.labels

    def ensure_label(self) -> None:
        status, data = self.imap.create(utf7.quoted(self._label))
        if status == "OK":
            return
        text = b" ".join(d for d in data if isinstance(d, bytes)).decode("utf-8", "replace").lower()
        if "exist" in text:
            return
        raise MailError(f"{self.config.name}: server rejected label/folder '{self._label}': {text}")

    def mark_processed(self, uids: list[str]) -> None:
        """Seen + label (Gmail) or Seen + move to the folder (other servers)."""
        if not uids:
            return
        self.ensure_label()
        self.select(readonly=False)
        uid_set = ",".join(uids)
        self._check(self.imap.uid("STORE", uid_set, "+FLAGS", r"(\Seen)"), "STORE \\Seen")
        if self.config.kind == "gmail":
            self._check(self.imap.uid("STORE", uid_set, "+X-GM-LABELS", f"({utf7.quoted(self._label)})"),
                        "add label")
            return
        capabilities = {c.decode().upper() if isinstance(c, bytes) else str(c).upper()
                        for c in getattr(self.imap, "capabilities", ())}
        if "MOVE" in capabilities:
            self._check(self.imap.uid("MOVE", uid_set, utf7.quoted(self._label)), "MOVE")
        else:
            self._check(self.imap.uid("COPY", uid_set, utf7.quoted(self._label)), "COPY")
            self._check(self.imap.uid("STORE", uid_set, "+FLAGS", r"(\Deleted)"), "STORE \\Deleted")
            if "UIDPLUS" in capabilities:
                self._check(self.imap.uid("EXPUNGE", uid_set), "UID EXPUNGE")
