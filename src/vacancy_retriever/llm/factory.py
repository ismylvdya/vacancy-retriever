from __future__ import annotations

from ..config import AppConfig, ClaudeSdkSettings
from .base import LlmProvider
from .claude_sdk import ClaudeSdkProvider
from .http_providers import AnthropicApiProvider, OpenAiApiProvider


def create_provider(config: AppConfig, claude_sdk: ClaudeSdkSettings | None = None) -> LlmProvider:
    """claude_sdk: other CLI settings for one task (e.g. more effort for the CV than for fact extraction)."""
    llm = config.llm
    if llm.provider == "claude_sdk":
        return ClaudeSdkProvider(claude_sdk or llm.claude_sdk, config.paths.llm_cwd)
    if llm.provider == "anthropic_api":
        return AnthropicApiProvider(llm.anthropic_api, config.secrets.get("ANTHROPIC_API_KEY"))
    return OpenAiApiProvider(llm.openai_api, config.secrets.get("OPENAI_API_KEY"))
