from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Any

from ..config import ClaudeSdkSettings
from .base import LlmError, LlmProvider, LlmResponse, LlmUsage, parse_json_object

log = logging.getLogger(__name__)

CLI_FALLBACKS = ("/opt/homebrew/bin/claude", "/usr/local/bin/claude", "~/.local/bin/claude", "~/.claude/local/claude")
_LIMIT_MARKERS = ("usage limit", "rate limit", "limit reached", "credit balance", "overloaded", "quota")
_AUTH_MARKERS = ("not logged in", "/login", "authentication", "invalid api key", "oauth")


def resolve_cli_path(configured: str | None) -> str | None:
    """The user's logged-in `claude` CLI: config → PATH → common install locations."""
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.exists() else None
    found = shutil.which("claude")
    if found:
        return found
    for candidate in CLI_FALLBACKS:
        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
    return None


def _classify(message: str) -> LlmError:
    lowered = message.lower()
    if any(marker in lowered for marker in _LIMIT_MARKERS):
        return LlmError(message, kind="limit", retryable=False)
    if any(marker in lowered for marker in _AUTH_MARKERS):
        return LlmError(message, kind="auth", retryable=False)
    return LlmError(message, kind="other")


class ClaudeSdkProvider(LlmProvider):
    """Claude via the logged-in Claude Code CLI (subscription), isolated from user settings."""

    name = "claude_sdk"

    def __init__(self, settings: ClaudeSdkSettings, cwd: Path):
        self._settings = settings
        self._cwd = cwd
        self._cli = resolve_cli_path(settings.cli_path)

    @property
    def cli_path(self) -> str | None:
        return self._cli

    def _options(self, system: str, model: str, schema: dict[str, Any] | None) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        self._cwd.mkdir(parents=True, exist_ok=True)
        return ClaudeAgentOptions(
            model=model,
            system_prompt=system,
            tools=[],
            allowed_tools=[],
            setting_sources=[],
            strict_mcp_config=True,
            mcp_servers={},
            permission_mode="default",
            cwd=str(self._cwd),
            cli_path=self._cli,
            output_format={"type": "json_schema", "schema": schema}
            if self._settings.structured_output and schema
            else None,
            effort=self._settings.effort,
            thinking={"type": self._settings.thinking} if self._settings.thinking else None,
            stderr=lambda line: log.debug("claude-cli: %s", line),
        )

    async def complete_json(
        self, system: str, user: str, model: str, schema: dict[str, Any] | None = None
    ) -> LlmResponse:
        if not self._cli:
            raise LlmError(
                "Claude Code CLI not found. Install it and log in (see docs/HANDBOOK.md).",
                kind="auth",
                retryable=False,
            )
        try:
            return await asyncio.wait_for(
                self._run(system, user, model, schema), timeout=self._settings.timeout_seconds
            )
        except TimeoutError as exc:
            raise LlmError(f"Claude CLI timed out after {self._settings.timeout_seconds}s", kind="network") from exc

    async def _run(self, system: str, user: str, model: str, schema: dict[str, Any] | None) -> LlmResponse:
        from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock, query

        auth_source: str | None = None
        text_parts: list[str] = []
        result: Any = None
        try:
            async for message in query(prompt=user, options=self._options(system, model, schema)):
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    auth_source = message.data.get("apiKeySource")
                elif isinstance(message, AssistantMessage):
                    if message.error:
                        raise _classify(str(message.error))
                    text_parts.extend(b.text for b in message.content if isinstance(b, TextBlock))
                elif isinstance(message, ResultMessage):
                    result = message
        except LlmError:
            raise
        except Exception as exc:
            raise _classify(f"Claude CLI error: {exc}") from exc
        if result is None:
            raise LlmError("Claude CLI returned no result", kind="other")
        usage = self._usage(result)
        if result.is_error:
            raise _classify(str(result.result or result.subtype))
        if result.structured_output is not None and isinstance(result.structured_output, dict):
            data = result.structured_output
        else:
            data = parse_json_object(result.result or "".join(text_parts))
        return LlmResponse(data=data, usage=usage, raw_text=result.result or "", auth_source=auth_source)

    @staticmethod
    def _usage(result: Any) -> LlmUsage:
        usage = result.usage or {}
        model_usage = result.model_usage or {}
        model = max(model_usage, key=lambda m: model_usage[m].get("outputTokens") or 0) if model_usage else None
        return LlmUsage(
            input_tokens=usage.get("input_tokens") or 0,
            output_tokens=usage.get("output_tokens") or 0,
            cache_read_tokens=usage.get("cache_read_input_tokens") or 0,
            cache_creation_tokens=usage.get("cache_creation_input_tokens") or 0,
            model=model,
        )
