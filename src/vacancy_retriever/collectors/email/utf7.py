"""IMAP modified UTF-7 (RFC 3501 §5.1.3) for mailbox / label names like "🦆 retrievered"."""

from __future__ import annotations

import base64


def _b64_encode(chunk: str) -> str:
    raw = chunk.encode("utf-16-be")
    return base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")


def encode(name: str) -> str:
    result: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            result.append("&" + _b64_encode("".join(pending)) + "-")
            pending.clear()

    for char in name:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            result.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(result)


def decode(name: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(name):
        char = name[index]
        if char != "&":
            result.append(char)
            index += 1
            continue
        end = name.index("-", index)
        chunk = name[index + 1 : end]
        if not chunk:
            result.append("&")
        else:
            padded = chunk.replace(",", "/") + "=" * (-len(chunk) % 4)
            result.append(base64.b64decode(padded).decode("utf-16-be"))
        index = end + 1
    return "".join(result)


def quoted(name: str) -> str:
    """Encoded and quoted, ready for IMAP commands (names with spaces must be quoted)."""
    encoded = encode(name).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{encoded}"'
