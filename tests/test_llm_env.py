from __future__ import annotations

from vacancy_retriever.llm.claude_sdk import scrub_environment


def test_scrub_environment_keeps_only_safe_variables():
    env = {
        "ANTHROPIC_API_KEY": "sk-x", "ANTHROPIC_BASE_URL": "http://proxy", "ANTHROPIC_AUTH_TOKEN": "t",
        "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "desktop", "CLAUDE_CONFIG_DIR": "/x",
        "PATH": "/usr/bin", "HOME": "/Users/u",
    }
    removed = scrub_environment(env)
    assert set(env) == {"CLAUDE_CONFIG_DIR", "PATH", "HOME"}
    assert "ANTHROPIC_API_KEY" in removed and "CLAUDECODE" in removed
