from __future__ import annotations

from ..config import AppConfig
from .base import LlmProvider
from .claude_sdk import ClaudeSdkProvider
from .http_providers import AnthropicApiProvider, OpenAiApiProvider


def create_provider(config: AppConfig) -> LlmProvider:
    llm = config.llm
    if llm.provider == "claude_sdk":
        return ClaudeSdkProvider(llm.claude_sdk, config.paths.llm_cwd)
    if llm.provider == "anthropic_api":
        return AnthropicApiProvider(llm.anthropic_api, config.secrets.get("ANTHROPIC_API_KEY"))
    return OpenAiApiProvider(llm.openai_api, config.secrets.get("OPENAI_API_KEY"))
