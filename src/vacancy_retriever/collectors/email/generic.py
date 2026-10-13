"""Universal e-mail parser: readable text + links of the letter, handed to the LLM as one multi-vacancy item."""

from __future__ import annotations

import re
from email.message import Message
from email.utils import parsedate_to_datetime
from datetime import UTC, datetime

from bs4 import BeautifulSoup

MAX_URL = 160
_WS = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")
_TRACKING = re.compile(r"[?&](utm_[^=&]+|trk[^=&]*|refId|trackingId|lipi|midToken|midSig|eid|otpToken)=[^&]*", re.I)


def clean_url(url: str) -> str:
    url = _TRACKING.sub("", url.strip())
    return url if len(url) <= MAX_URL else url.split("?", 1)[0]


def _part_text(message: Message, subtype: str) -> str | None:
    for part in message.walk():
        if part.get_content_type() == f"text/{subtype}" and not part.get_filename():
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="replace")
            except LookupError:
                return payload.decode("utf-8", errors="replace")
    return None


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "head", "title"]):
        tag.decompose()
    for link in soup.find_all("a", href=True):
        href = link["href"]
        label = link.get_text(" ", strip=True)
        if href.startswith("http") and label:
            link.replace_with(f"{label} ({clean_url(href)})")
        elif href.startswith("http"):
            link.replace_with(f" {clean_url(href)} ")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table"]):
        block.insert_after("\n")
    return normalize(soup.get_text())


def normalize(text: str) -> str:
    lines = [_WS.sub(" ", line).strip() for line in text.splitlines()]
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def message_text(message: Message) -> str:
    html = _part_text(message, "html")
    if html:
        return html_to_text(html)
    plain = _part_text(message, "plain") or ""
    return normalize(re.sub(r"https?://\S+", lambda m: clean_url(m.group(0)), plain))


def message_date(message: Message) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(message.get("Date", ""))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def decoded_header(message: Message, name: str) -> str:
    from email.header import decode_header, make_header

    try:
        return str(make_header(decode_header(message.get(name, ""))))
    except (UnicodeDecodeError, LookupError):
        return message.get(name, "")


def parse_generic(message: Message) -> str:
    subject = decoded_header(message, "Subject")
    return f"Subject: {subject}\n\n{message_text(message)}"
