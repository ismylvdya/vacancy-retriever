import asyncio
import json

import pytest

from vacancy_retriever.config import CodexCliSettings
from vacancy_retriever.llm.base import LlmError
from vacancy_retriever.llm.codex_cli import CodexCliProvider


def fake_cli(tmp_path, events, exit_code=0):
    cli = tmp_path / "codex"
    cli.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                   "assert json.load(sys.stdin)['input'] == 'vacancy'\n"
                   "assert 'OPENAI_API_KEY' not in os.environ\n"
                   "assert 'CODEX_API_KEY' not in os.environ\n"
                   f"print({chr(10).join(map(json.dumps, events))!r})\n"
                   f"sys.exit({exit_code})\n")
    cli.chmod(0o700)
    return CodexCliProvider(CodexCliSettings(cli_path=str(cli)), tmp_path, "large")


def test_subscription_json_and_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("CODEX_API_KEY", "test")
    cli = fake_cli(tmp_path, [
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"role":"analyst"}'}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 5}},
    ])
    reply = asyncio.run(cli.complete_json("Extract facts", "vacancy", "small"))
    assert reply.data == {"role": "analyst"}
    assert (reply.usage.input_tokens, reply.usage.cache_read_tokens, reply.usage.output_tokens) == (20, 80, 5)


@pytest.mark.parametrize(("message", "kind"), [("Usage limit reached", "limit"), ("Not logged in", "auth")])
def test_subscription_error(tmp_path, message, kind):
    cli = fake_cli(tmp_path, [{"type": "turn.failed", "error": {"message": message}}], exit_code=1)
    with pytest.raises(LlmError) as failure:
        asyncio.run(cli.complete_json("Extract facts", "vacancy", "small"))
    assert failure.value.kind == kind
    assert not failure.value.retryable
