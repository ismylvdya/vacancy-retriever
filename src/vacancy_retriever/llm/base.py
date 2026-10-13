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


@dataclass
class LlmUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    model: str | None = None

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


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse the JSON object from a model reply, tolerating code fences and surrounding prose."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise LlmError(f"No JSON object in model reply: {text[:200]!r}", kind="parse") from None
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LlmError(f"Invalid JSON in model reply: {exc}", kind="parse") from exc
    if not isinstance(value, dict):
        raise LlmError("Model reply JSON is not an object", kind="parse")
    return value
