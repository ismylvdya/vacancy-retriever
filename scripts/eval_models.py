"""Сравнение моделей на размеченном наборе samples/eval/ (50 реальных вакансий, разметка агента).

    .venv/bin/python scripts/eval_models.py --models haiku,sonnet

Тратит лимит подписки Claude (~50 вакансий на модель). Результаты: таблица в консоли и
.cache/eval/results_<model>.json; итоговые выводы — в EVAL.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vacancy_retriever.config import AppConfig, ClaudeSdkSettings  # noqa: E402
from vacancy_retriever.llm.base import LlmResponse  # noqa: E402
from vacancy_retriever.llm.claude_sdk import ClaudeSdkProvider  # noqa: E402
from vacancy_retriever.pipeline.llm_stage import VacancyScorer, parse_scoring_response, run_jobs  # noqa: E402
from vacancy_retriever.scoring.engine import ScoringEngine  # noqa: E402
from vacancy_retriever.storage.items import StoredRawItem  # noqa: E402
from vacancy_retriever.timeutil import from_iso, utcnow  # noqa: E402

MODEL_SETTINGS = {
    "haiku": ClaudeSdkSettings(effort="low", thinking="disabled"),
    "sonnet": ClaudeSdkSettings(effort="low"),
}


@dataclass
class Usage:
    calls: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    seconds: float = 0.0
    failures: list[str] = field(default_factory=list)


class RecordingCaller:
    def __init__(self, provider: ClaudeSdkProvider, usage: Usage):
        self.provider = provider
        self.usage = usage

    async def call(self, stage: str, system: str, user: str, model: str, n_items: int) -> LlmResponse:
        started = time.monotonic()
        response = await self.provider.complete_json(system, user, model)
        u = response.usage
        self.usage.calls += 1
        self.usage.input += u.input_tokens or 0
        self.usage.output += u.output_tokens or 0
        self.usage.cache_read += u.cache_read_tokens or 0
        self.usage.cache_write += u.cache_creation_tokens or 0
        self.usage.seconds += time.monotonic() - started
        return response


def load_items() -> list[StoredRawItem]:
    raw = json.loads((ROOT / "samples" / "eval" / "items.json").read_text(encoding="utf-8"))
    return [StoredRawItem(r["id"], 0, True, r["source_id"], str(r["id"]), "", r["title"], r["text"], None,
                          from_iso(r["published_at"]), r["multi"], r["meta"], "pending_llm", None) for r in raw]


def outcome(verdicts: list) -> tuple[str, int | None, int | None]:
    shown = [v for v in verdicts if v.decision == "show"]
    if shown:
        best = max(shown, key=lambda v: (-(v.group or 9), v.stars or 0))
        return "show", best.group, best.stars
    if any(v.decision == "skip" for v in verdicts):
        return "skip", None, None
    return "hidden", None, None


def evaluate(model: str, config: AppConfig, items: list[StoredRawItem], labels: dict) -> dict:
    usage = Usage()
    provider = ClaudeSdkProvider(MODEL_SETTINGS[model], config.paths.llm_cwd)
    scorer = VacancyScorer(config, RecordingCaller(provider, usage), {})
    batches = scorer.batches(items)

    async def score(batch: list[StoredRawItem]) -> list:
        response = await scorer._caller.call("scoring", scorer._system, scorer.user_prompt(batch), model, len(batch))
        return parse_scoring_response(response.data, {i.id for i in batch})

    import threading

    results = run_jobs([lambda b=b: score(b) for _, b in batches], 2, threading.Event())
    engine = ScoringEngine(config.rules)
    now = utcnow()
    by_item = {item.id: item for item in items}
    rows = {}
    for result in results:
        if isinstance(result, BaseException):
            usage.failures.append(str(result))
            continue
        for scored in result:
            item = by_item[scored.raw_id]
            verdicts = [engine.evaluate(f, item.published_at, now) for f in scored.vacancies]
            decision, group, stars = outcome(verdicts)
            rows[scored.raw_id] = {"decision": decision, "group": group, "stars": stars,
                                   "facts": [f.model_dump() for f in scored.vacancies]}
    return {"model": model, "rows": rows, "usage": usage.__dict__, "metrics": metrics(rows, labels, usage)}


def metrics(rows: dict, labels: dict, usage: Usage) -> dict:
    decision_ok = group_ok = group_total = 0
    deltas: list[int] = []
    false_skips: list[str] = []
    mismatches: list[str] = []
    for key, label in labels.items():
        if key.startswith("_"):
            continue
        row = rows.get(int(key))
        if row is None:
            mismatches.append(f"{key}: no answer")
            if label.get("good"):
                false_skips.append(f"{key} (no answer)")
            continue
        if row["decision"] in label["decision"]:
            decision_ok += 1
        else:
            mismatches.append(f"{key}: {row['decision']} vs {label['decision']} — {label.get('note', '')}")
        if label.get("good") and row["decision"] != "show":
            false_skips.append(f"{key} {label.get('note', '')}")
        if row["decision"] == "show" and label.get("group") and "show" in label["decision"]:
            group_total += 1
            group_ok += int(row["group"] in label["group"])
            if row["group"] not in label["group"]:
                mismatches.append(f"{key}: group {row['group']} vs {label['group']} — {label.get('note', '')}")
            if label.get("stars") is not None and row["stars"] is not None:
                deltas.append(abs(row["stars"] - label["stars"]))
    total = len([k for k in labels if not k.startswith("_")])
    tokens = usage.input + usage.output + usage.cache_read + usage.cache_write
    return {
        "decision_agreement": f"{decision_ok}/{total}",
        "group_agreement": f"{group_ok}/{group_total}",
        "mean_abs_star_delta": round(sum(deltas) / len(deltas), 2) if deltas else None,
        "false_skips": false_skips,
        "tokens_per_item": round(tokens / total),
        "output_per_item": round(usage.output / total),
        "seconds": round(usage.seconds),
        "mismatches": mismatches,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="haiku,sonnet")
    args = parser.parse_args()
    config = AppConfig()
    items = load_items()
    labels = json.loads((ROOT / "samples" / "eval" / "labels.json").read_text(encoding="utf-8"))
    out = ROOT / ".cache" / "eval"
    out.mkdir(parents=True, exist_ok=True)
    for model in args.models.split(","):
        result = evaluate(model, config, items, labels)
        (out / f"results_{model}.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str))
        print(f"\n=== {model} ===")
        print(json.dumps(result["metrics"], ensure_ascii=False, indent=1))
        print("usage:", {k: v for k, v in result["usage"].items() if k != "failures"}, "failures:",
              result["usage"]["failures"])


if __name__ == "__main__":
    main()
