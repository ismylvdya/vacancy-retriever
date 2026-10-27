"""Bytes the app itself downloads during a run (P2, 28.10.2026: "how many MB of internet a run took").

Counted where the app talks to the network: the shared HTTP client (sites, Telegram previews, icons, rates),
the job-search API, the LinkedIn guest pages and IMAP. Claude CLI traffic is not visible to the app and is not
counted. Bodies and headers of responses only (requests are tiny), so this is a close lower bound.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_total = 0


def add(n: int) -> None:
    global _total
    with _lock:
        _total += max(n, 0)


def response_size(response) -> int:
    """httpx response: body + headers (approximately, as they came over the wire)."""
    headers = sum(len(k) + len(v) + 4 for k, v in response.headers.raw)
    return len(response.content) + headers


def total() -> int:
    with _lock:
        return _total
