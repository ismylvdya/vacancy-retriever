from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..timeutil import to_iso, utcnow
from .db import Database


@dataclass
class LlmCallRecord:
    run_id: int | None
    stage: str
    provider: str
    model: str | None
    n_items: int
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    ok: bool
    duration_s: float
    error: str | None = None
    tokens_unknown: bool = False
    auth_source: str | None = None


class LlmCallRepository:
    def __init__(self, db: Database, token_log: Path | None):
        self.db = db
        self.token_log = token_log

    def record(self, call: LlmCallRecord) -> None:
        created = to_iso(utcnow())
        self.db.execute(
            "INSERT INTO llm_calls(run_id, stage, provider, model, n_items, input_tokens, output_tokens,"
            " cache_read_tokens, cache_creation_tokens, tokens_unknown, ok, error, duration_s, auth_source, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (call.run_id, call.stage, call.provider, call.model, call.n_items, call.input_tokens, call.output_tokens,
             call.cache_read_tokens, call.cache_creation_tokens, int(call.tokens_unknown), int(call.ok), call.error,
             round(call.duration_s, 2), call.auth_source, created),
        )
        if self.token_log:
            self.token_log.parent.mkdir(parents=True, exist_ok=True)
            with self.token_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"created_at": created, **asdict(call)}, ensure_ascii=False) + "\n")

    def by_run(self, run_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM llm_calls WHERE run_id = ? ORDER BY id", (run_id,))]

    def all(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM llm_calls ORDER BY id")]

    def last_auth_source(self) -> str | None:
        row = self.db.query_one(
            "SELECT auth_source FROM llm_calls WHERE auth_source IS NOT NULL ORDER BY id DESC LIMIT 1"
        )
        return row["auth_source"] if row else None
