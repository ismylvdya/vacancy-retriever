from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy

import pytest

from vacancy_retriever.collectors.base import CollectContext, SourceCursor, SourceError, SourceNotConfigured
from vacancy_retriever.collectors.email import utf7
from vacancy_retriever.collectors.email.collector import EmailCollector, items_from_message, sender_rule
from vacancy_retriever.collectors.email.generic import parse_generic
from vacancy_retriever.collectors.email.imap import Mailbox, MailError
from vacancy_retriever.collectors.email.parsers import PARSERS, load_parsers
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.config import AppConfig, MailboxConfig, SenderRule, SourceConfig

from .conftest import SAMPLES

LABEL = "🦆 retrievered"


def test_modified_utf7_round_trip_of_duck_label():
    encoded = utf7.encode(LABEL)
    assert encoded == "&2D7dhg- retrievered"
    assert utf7.decode(encoded) == LABEL
    assert utf7.quoted(LABEL) == '"&2D7dhg- retrievered"'


@pytest.mark.parametrize("name", ["INBOX", "Вакансии", "a&b", "Ярлык 🦆 2", "日本語"])
def test_modified_utf7_round_trip(name):
    assert utf7.decode(utf7.encode(name)) == name
    assert all(0x20 <= ord(c) <= 0x7E for c in utf7.encode(name))


def make_letter(sender: str = "LinkedIn <jobalerts-noreply@linkedin.com>", uid_hint: str = "1") -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = "Новые вакансии: Junior Data Scientist"
    message["Message-ID"] = f"<msg-{uid_hint}@example.com>"
    message["Date"] = "Mon, 12 Oct 2026 09:00:00 +0200"
    message.set_content("plain")
    message.add_alternative(
        "<html><body><table><tr><td><a href='https://www.linkedin.com/comm/jobs/view/123?trackingId=abc&refId=x'>"
        "Junior Data Scientist</a></td></tr><tr><td>Acme · Belgrade</td></tr></table></body></html>",
        subtype="html",
    )
    return message


def test_generic_parser_keeps_text_and_clean_links():
    text = parse_generic(make_letter())
    assert text.startswith("Subject: Новые вакансии")
    assert "Junior Data Scientist (https://www.linkedin.com/comm/jobs/view/123)" in text
    assert "Acme · Belgrade" in text


def test_items_from_generic_letter():
    rule = SenderRule(match="linkedin.com", name="LinkedIn", parser="generic")
    items = items_from_message("gmail", "gmail", "7", make_letter(), rule)
    assert len(items) == 1
    item = items[0]
    assert item.multi and item.url == ""
    assert item.external_id == "<msg-1@example.com>"
    assert item.meta["email"]["uid"] == "7"
    assert item.published_at == datetime(2026, 10, 12, 7, 0, tzinfo=UTC)


def test_sender_matching():
    rules = [SenderRule(match="hh.ru", name="hh"), SenderRule(match="linkedin.com", name="LinkedIn")]
    assert sender_rule(make_letter(), rules).name == "LinkedIn"
    assert sender_rule(make_letter("noreply@other.org"), rules) is None


class FakeImap:
    """Minimal imaplib.IMAP4 stand-in recording every command."""

    def __init__(self, host: str, port: int, letters: dict[str, bytes], reject_create: bool = False,
                 labels: dict[str, str] | None = None):
        self.letters = letters
        self.labels = labels or {}
        self.reject_create = reject_create
        self.commands: list[tuple] = []
        self.capabilities = ("IMAP4REV1", "MOVE")

    def login(self, user, password):
        self.commands.append(("LOGIN",))
        return "OK", [b"ok"]

    def logout(self):
        return "BYE", []

    def select(self, mailbox, readonly=False):
        self.commands.append(("SELECT", mailbox, readonly))
        return "OK", [b"1"]

    def create(self, mailbox):
        self.commands.append(("CREATE", mailbox))
        if self.reject_create:
            return "NO", [b"[CANNOT] Invalid mailbox name"]
        return "NO", [b"[ALREADYEXISTS] Mailbox exists"]

    def uid(self, command, *args):
        self.commands.append((command, *args))
        if command == "SEARCH":
            return "OK", [" ".join(self.letters).encode()]
        if command == "FETCH":
            uid = args[0]
            header = f'{uid} (UID {uid} X-GM-LABELS ({self.labels.get(uid, "")}) BODY[] {{1}}'.encode()
            return "OK", [(header, self.letters[uid]), b")"]
        return "OK", [b"done"]


def mailbox_config(kind: str = "gmail") -> MailboxConfig:
    return MailboxConfig(id="gmail", name="Gmail", kind=kind, host="imap.example.com", login_env="L", password_env="P")


def test_mark_processed_gmail_quotes_encoded_label():
    imap = FakeImap("h", 993, {})
    with Mailbox(mailbox_config(), "u", "p", LABEL, factory=lambda h, p: imap) as mailbox:
        mailbox.mark_processed(["3", "5"])
    assert ("CREATE", '"&2D7dhg- retrievered"') in imap.commands
    assert ("STORE", "3,5", "+FLAGS", r"(\Seen)") in imap.commands
    assert ("STORE", "3,5", "+X-GM-LABELS", '("&2D7dhg- retrievered")') in imap.commands


def test_mark_processed_yandex_moves_to_folder():
    imap = FakeImap("h", 993, {})
    with Mailbox(mailbox_config("yandex"), "u", "p", LABEL, factory=lambda h, p: imap) as mailbox:
        mailbox.mark_processed(["9"])
    assert ("MOVE", "9", '"&2D7dhg- retrievered"') in imap.commands


def test_rejected_label_is_a_clear_error():
    imap = FakeImap("h", 993, {}, reject_create=True)
    with pytest.raises(MailError, match="rejected label"):
        with Mailbox(mailbox_config(), "u", "p", LABEL, factory=lambda h, p: imap) as mailbox:
            mailbox.mark_processed(["1"])


@pytest.fixture
def email_collector(tmp_paths, monkeypatch):
    (tmp_paths.root / "config_env").write_text("")
    config = AppConfig(tmp_paths)
    monkeypatch.setattr("vacancy_retriever.collectors.email.collector.AppConfig", lambda: config)
    config.__dict__["secrets"] = type(config.secrets)({"GMAIL_LOGIN": "me", "GMAIL_APP_PASSWORD": "pw"})
    letters = {"11": make_letter(uid_hint="11").as_bytes(), "12": make_letter(uid_hint="12").as_bytes()}
    imap = FakeImap("h", 993, letters, labels={"12": '"\\\\Inbox" "&2D7dhg- retrievered"'})
    monkeypatch.setattr(EmailCollector, "imap_factory", staticmethod(lambda h, p: imap))
    source = SourceConfig(id="gmail", type="email", name="Gmail", params={"mailbox": "gmail"})
    return EmailCollector(source, PoliteHttpClient(0, 0)), imap, config


def test_collect_reads_without_changing_mailbox(email_collector):
    collector, imap, _ = email_collector
    ctx = CollectContext(SourceCursor(), datetime.now(UTC) - timedelta(days=30), 1, lambda _id: False,
                         threading.Event())
    result = collector.collect(ctx)
    assert [i.meta["email"]["uid"] for i in result.items] == ["11"]
    assert ("SELECT", '"INBOX"', True) in imap.commands
    assert all(c[0] not in ("STORE", "MOVE", "COPY", "CREATE") for c in imap.commands)
    assert any(c[0] == "FETCH" and "BODY.PEEK[]" in c[2] for c in imap.commands)


def test_after_commit_labels_processed_letters(email_collector):
    collector, imap, _ = email_collector
    errors = collector.after_commit([{"email": {"mailbox": "gmail", "uid": "11"}}])
    assert errors == []
    assert ("STORE", "11", "+X-GM-LABELS", '("&2D7dhg- retrievered")') in imap.commands


def test_missing_credentials_is_not_configured(tmp_paths, monkeypatch):
    config = AppConfig(tmp_paths)
    config.__dict__["secrets"] = type(config.secrets)({})
    monkeypatch.setattr("vacancy_retriever.collectors.email.collector.AppConfig", lambda: config)
    collector = EmailCollector(SourceConfig(id="gmail", type="email", name="Gmail"), PoliteHttpClient(0, 0))
    ctx = CollectContext(SourceCursor(), datetime.now(UTC), 1, lambda _id: False)
    with pytest.raises(SourceNotConfigured):
        collector.collect(ctx)


def test_unknown_mailbox_is_an_error(tmp_paths, monkeypatch):
    config = AppConfig(tmp_paths)
    monkeypatch.setattr("vacancy_retriever.collectors.email.collector.AppConfig", lambda: config)
    collector = EmailCollector(SourceConfig(id="x", type="email", name="X", params={"mailbox": "nope"}),
                               PoliteHttpClient(0, 0))
    with pytest.raises(SourceError):
        collector.collect(CollectContext(SourceCursor(), datetime.now(UTC), 1, lambda _id: False))


def test_sample_emails(config):
    """Every .eml in samples/emails/<sender>/ must produce at least one item with its configured parser."""
    load_parsers()
    for path in sorted((SAMPLES / "emails").glob("*/*.eml")):
        message = message_from_bytes(path.read_bytes(), policy=default_policy)
        rule = sender_rule(message, config.mail.senders) or SenderRule(match="", name=path.parent.name)
        if rule.parser != "generic":
            assert rule.parser in PARSERS, f"{path.name}: parser '{rule.parser}' is not registered"
        items = items_from_message("sample", "sample", "1", message, rule)
        assert items and all(item.text.strip() for item in items), path.name
