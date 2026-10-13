from __future__ import annotations

from typing import Any

import httpx

from ..config import ApiSettings
from .base import LlmError, LlmProvider, LlmResponse, LlmUsage, parse_json_object

ANTHROPIC_VERSION = "2023-06-01"


def _http_error(provider: str, response: httpx.Response) -> LlmError:
    status = response.status_code
    detail = response.text[:300]
    if status in (401, 403):
        return LlmError(f"{provider}: authentication failed ({status}). Check the API key in config/.env.", "auth", False)
    if status == 429:
        return LlmError(f"{provider}: rate/usage limit (429): {detail}", "limit", True)
    if status >= 500:
        return LlmError(f"{provider}: server error {status}", "network", True)
    return LlmError(f"{provider}: HTTP {status}: {detail}", "other", False)


class _HttpProvider(LlmProvider):
    def __init__(self, settings: ApiSettings, api_key: str | None, transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise LlmError(f"{self.name}: API key is missing in config/.env", "auth", False)
        self._settings = settings
        self._api_key = api_key
        self._transport = transport

    async def _post(self, path: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self._settings.base_url, timeout=self._settings.timeout_seconds, transport=self._transport
            ) as client:
                response = await client.post(path, headers=headers, json=body)
        except httpx.TransportError as exc:
            raise LlmError(f"{self.name}: network error: {exc}", "network") from exc
        if response.status_code >= 400:
            raise _http_error(self.name, response)
        return response.json()


class AnthropicApiProvider(_HttpProvider):
    """Anthropic Messages API (paid). The stable system prompt is cached."""

    name = "anthropic_api"

    async def complete_json(
        self, system: str, user: str, model: str, schema: dict[str, Any] | None = None
    ) -> LlmResponse:
        body = {
            "model": model,
            "max_tokens": 16000,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
        }
        headers = {"x-api-key": self._api_key, "anthropic-version": ANTHROPIC_VERSION}
        payload = await self._post("/v1/messages", headers, body)
        if payload.get("stop_reason") == "refusal":
            raise LlmError("anthropic_api: model refused the request", "other", False)
        text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
        usage = payload.get("usage") or {}
        return LlmResponse(
            data=parse_json_object(text),
            usage=LlmUsage(
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                model=payload.get("model", model),
            ),
            raw_text=text,
            auth_source="api_key",
        )


class OpenAiApiProvider(_HttpProvider):
    """OpenAI Chat Completions API (paid), JSON mode. OpenAI caches long prefixes automatically."""

    name = "openai_api"

    async def complete_json(
        self, system: str, user: str, model: str, schema: dict[str, Any] | None = None
    ) -> LlmResponse:
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        payload = await self._post("/v1/chat/completions", headers, body)
        choices = payload.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        usage = payload.get("usage") or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return LlmResponse(
            data=parse_json_object(text),
            usage=LlmUsage(
                input_tokens=(usage.get("prompt_tokens") or 0) - cached,
                output_tokens=usage.get("completion_tokens", 0),
                cache_read_tokens=cached,
                cache_creation_tokens=0,
                model=payload.get("model", model),
            ),
            raw_text=text,
            auth_source="api_key",
        )
