from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


class LlmError(Exception):
    """LLM call failed. `kind`: limit | network | auth | parse | other."""

    def __init__(self, message: str, kind: str = "other", retryable: bool = True):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.usage: LlmUsage | None = None  # tokens the failed call still spent (e.g. a reply that is not JSON)
        self.raw_text: str | None = None    # the reply itself, for diagnosis


@dataclass
class LlmUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    model: str | None = None
    # Claude subscription after this call (claude_sdk only): share of the 5-hour / weekly limit used, 0–1, and when
    # the 5-hour window resets (unix time) — the ground truth for "how much of the limit a run takes"
    five_hour: float | None = None
    seven_day: float | None = None
    five_hour_resets_at: int | None = None

    @property
    def known(self) -> bool:
        return self.input_tokens is not None


@dataclass
class LlmResponse:
    data: dict[str, Any]
    usage: LlmUsage
    raw_text: str
    auth_source: str | None = None


class LlmProvider(ABC):
    name: str

    @abstractmethod
    async def complete_json(
        self, system: str, user: str, model: str, schema: dict[str, Any] | None = None
    ) -> LlmResponse: ...


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


def _repair_quotes(text: str, attempts: int = 40) -> Any:
    """The usual way a model breaks JSON: a quote inside a string left unescaped («company "Pulse" hires»).
    The decoder stops right after such a quote expecting ',' or '}' — escape the quote it stopped after and retry."""
    for _ in range(attempts):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            if not exc.msg.startswith(("Expecting ',' delimiter", "Expecting ':' delimiter")):
                raise
            quote = text.rfind('"', 0, exc.pos)
            if quote <= 0 or text[quote - 1] == "\\":
                raise
            text = text[:quote] + '\\"' + text[quote + 1:]
    return json.loads(text)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse the JSON object from a model reply, tolerating code fences, surrounding prose and unescaped quotes."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise LlmError(f"No JSON object in model reply: {text[:200]!r}", kind="parse") from None
        try:
            value = _repair_quotes(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LlmError(f"Invalid JSON in model reply: {exc}", kind="parse") from exc
    if not isinstance(value, dict):
        raise LlmError("Model reply JSON is not an object", kind="parse")
    return value
