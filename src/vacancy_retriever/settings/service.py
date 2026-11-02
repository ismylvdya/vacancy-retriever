from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from ..collectors.egress import Egress, EgressError
from ..collectors.linkedin_guest import create_egress
from ..config import ApplyConfig, AppConfig, LlmConfig, MailConfig, Rules, SourcesFile
from ..scoring.engine import ScoringEngine
from ..scoring.facts import VacancyFacts
from .schema import CONFIG_FILES, KINDS, SECTIONS, Field, all_fields
from .yaml_patch import PatchError, YamlDocument

MODELS: dict[str, type[BaseModel]] = {"rules": Rules, "sources": SourcesFile, "mail": MailConfig, "llm": LlmConfig,
                                      "apply": ApplyConfig}
_CATEGORY = re.compile(r"^rules:categories\.(\w+)\.(kind|base|cap|drop_reason)$")
LINKEDIN_SWITCH = "linkedin_public_pages.enabled"  # never a Field: the Save button of the screen cannot reach it
LINKEDIN_CONFIRM = "ENABLE LINKEDIN"
_GROUP = re.compile(r"^rules:groups\.([1-5])\.(title|note|collapsed)$")
_SOURCE = re.compile(r"^sources:sources\[([\w.-]+)\]\.enabled$")
STACKS = (("strong", "Stack matches"), ("partial", "Partly"), ("none", "Does not match"))


class SettingsError(Exception):
    def __init__(self, problems: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in problems.items()))
        self.problems = problems


@dataclass
class SaveResult:
    files: list[str]
    rules_changed: bool
    problems: dict[str, str] = field(default_factory=dict)


class LinkedInSwitchError(Exception):
    """LinkedIn public pages stay off: the message says why."""


class SettingsService:
    """Reads config/*.yaml for the settings screen and writes changes back, value by value.

    Every change is checked twice before a file is replaced: the edited value must be what was asked (yaml_patch),
    and the whole file must pass the same pydantic model as at start-up. Nothing is written when anything fails."""

    def __init__(self, config: AppConfig, egress_factory: Callable[[AppConfig], Egress | None] = create_egress):
        self._config = config
        self._egress_factory = egress_factory

    def _path(self, file: str) -> Path:
        return self._config.paths.config / f"{file}.yaml"

    def _documents(self) -> dict[str, YamlDocument]:
        return {name: YamlDocument(self._path(name).read_text(encoding="utf-8")) for name in CONFIG_FILES}

    # ---------- reading
    def snapshot(self) -> dict[str, Any]:
        docs = self._documents()
        values = {f.id: _plain(docs[f.file].get(f.path)) for f in all_fields().values()}
        rules = self._config.rules
        categories = [{"key": key, "label": rule.label, "kind": rule.kind, "base": rule.base, "cap": rule.cap,
                       "drop_reason": rule.drop_reason or ""} for key, rule in rules.categories.items()]
        groups = [{"number": n, "title": g.title, "note": g.note or "", "collapsed": g.collapsed}
                  for n, g in sorted(rules.groups.items())]
        sources = [{"id": s.id, "name": s.name, "type": s.type, "enabled": s.enabled} for s in self._config.sources.sources]
        return {"sections": [s.as_dict() for s in SECTIONS], "values": values, "categories": categories,
                "groups": groups, "sources": sources, "kinds": [{"value": v, "label": label} for v, label in KINDS],
                "fit": self._fit(rules), "linkedin_pages": self.linkedin_pages()}

    # ---------- writing
    def save(self, changes: dict[str, Any]) -> SaveResult:
        docs, touched = self._apply(changes)
        for name in touched:
            self._replace(name, docs[name].text)
        return SaveResult(sorted(touched), "rules" in touched)

    def _replace(self, name: str, text: str) -> None:
        path = self._path(name)
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{name}.", suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)  # atomic: a crash never leaves half a config
        self._config.__dict__.pop(name, None)  # AppConfig caches each file (cached_property)

    # ---------- the LinkedIn pages switch: not a field of the screen, it has its own two doors
    def linkedin_pages(self) -> dict[str, Any]:
        fresh = AppConfig(self._config.paths)  # config/.env may have changed since the server started
        egress = self._egress_factory(fresh)
        return {"enabled": fresh.mail.linkedin_public_pages.enabled, "exit": egress.label if egress else "",
                "confirm": LINKEDIN_CONFIRM}

    def linkedin_pages_off(self) -> None:
        """Always possible, needs nothing and touches no network."""
        self._write_linkedin_switch(False)

    def linkedin_pages_on(self, confirm: str) -> None:
        """Only with the phrase typed by hand and a private exit that works right now and is not the home network.
        The exit is checked through IP echo services; LinkedIn itself is not contacted here."""
        if confirm != LINKEDIN_CONFIRM:
            raise LinkedInSwitchError(f"Type {LINKEDIN_CONFIRM} to turn this on")
        egress = self._egress_factory(AppConfig(self._config.paths))
        if egress is None:
            raise LinkedInSwitchError("No proxy configured: set LINKEDIN_PROXY_URL in config/.env first")
        try:
            egress.start()
        except EgressError as exc:
            raise LinkedInSwitchError(f"The proxy did not pass its check: {exc}") from exc
        finally:
            egress.stop()
        self._write_linkedin_switch(True)

    def _write_linkedin_switch(self, enabled: bool) -> None:
        document = self._documents()["mail"]
        document.set(LINKEDIN_SWITCH, enabled)
        MODELS["mail"].model_validate(yaml.safe_load(document.text))
        self._replace("mail", document.text)

    def preview_fit(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Stars of every fact combination with unsaved changes of the rules (nothing is written)."""
        docs, _ = self._apply({k: v for k, v in changes.items() if k.startswith("rules:")})
        return self._fit(Rules.model_validate(yaml.safe_load(docs["rules"].text)))

    def _apply(self, changes: dict[str, Any]) -> tuple[dict[str, YamlDocument], set[str]]:
        docs = self._documents()
        problems: dict[str, str] = {}
        touched: set[str] = set()
        for field_id, raw in changes.items():
            try:
                file, path, value = self._resolve(field_id, raw)
                docs[file].set(path, value)
                touched.add(file)
            except (PatchError, ValueError) as exc:
                problems[field_id] = str(exc)
        for name in touched:
            try:
                MODELS[name].model_validate(yaml.safe_load(docs[name].text))
            except ValidationError as exc:
                for error in exc.errors():
                    problems[f"{name}:{'.'.join(str(p) for p in error['loc'])}"] = error["msg"]
        if problems:
            raise SettingsError(problems)
        return docs, touched

    def _resolve(self, field_id: str, raw: Any) -> tuple[str, str, Any]:
        known = all_fields().get(field_id)
        if known:
            return known.file, known.path, _coerce(known, raw)
        file, _, path = field_id.partition(":")
        if match := _CATEGORY.match(field_id):
            if match.group(1) not in self._config.rules.categories:
                raise ValueError("unknown category")
            kind = {"kind": "choice", "base": "stars", "cap": "stars", "drop_reason": "text"}[match.group(2)]
            options = KINDS if kind == "choice" else ()
            return file, path, _coerce(Field(file, path, kind, "", options=options), raw)  # type: ignore[arg-type]
        if match := _GROUP.match(field_id):
            kind = "toggle" if match.group(2) == "collapsed" else "text"
            return file, path, _coerce(Field(file, path, kind, ""), raw)  # type: ignore[arg-type]
        if match := _SOURCE.match(field_id):
            if not any(s.id == match.group(1) for s in self._config.sources.sources):
                raise ValueError("unknown source")
            return file, path, _coerce(Field(file, path, "toggle", ""), raw)
        raise ValueError("this setting cannot be changed here")

    # ---------- the fit matrix
    @staticmethod
    def _fit(rules: Rules) -> dict[str, Any]:
        engine = ScoringEngine(rules)
        threshold = rules.penalties.experience_years_threshold
        result: dict[str, Any] = {}
        for key, rule in rules.categories.items():
            if rule.kind not in ("it", "manual"):
                continue
            stacks = STACKS if rule.kind == "it" else (("n_a", ""),)
            englishes = (False, True) if rule.kind == "it" else (False,)
            rows = []
            for stack, stack_label in stacks:
                cells = []
                for english in englishes:
                    for experienced in (False, True):
                        facts = VacancyFacts(role_category=key, stack_overlap=stack, english_working=english,
                                             experience_years=threshold if experienced else None)
                        stars, steps = engine.fit(facts)
                        cells.append({"english": english, "experienced": experienced, "stars": stars, "why": steps})
                rows.append({"stack": stack, "label": stack_label, "cells": cells})
            result[key] = {"kind": rule.kind, "rows": rows, "english_axis": rule.kind == "it",
                           "threshold": threshold}
        return result


def _plain(value: Any) -> Any:
    if isinstance(value, list):
        return [str(v) for v in value]
    return value


def _coerce(f: Field, raw: Any) -> Any:
    if f.kind == "toggle":
        if not isinstance(raw, bool):
            raise ValueError("must be on or off")
        return raw
    if f.kind in ("number", "stars"):
        if raw in (None, "") and (f.nullable or f.kind == "stars"):
            return None
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("must be a number") from exc
        low, high = (1, 5) if f.kind == "stars" else (f.min, f.max)
        if (low is not None and number < low) or (high is not None and number > high):
            raise ValueError(f"must be between {low:g} and {high:g}" if high is not None else f"must be at least {low:g}")
        return int(number) if number.is_integer() else number
    if f.kind == "choice":
        if f.options and raw not in {v for v, _ in f.options}:
            raise ValueError("not one of the choices")
        return str(raw)
    if f.kind == "chips":
        if not isinstance(raw, list):
            raise ValueError("must be a list")
        items = list(dict.fromkeys(str(x).strip().lower() for x in raw if str(x).strip()))
        allowed = {v for v, _ in f.options}
        if allowed and not set(items) <= allowed:
            raise ValueError("unknown choice in the list")
        return items
    text = str(raw or "").strip()
    if "\n" in text or len(text) > 120:
        raise ValueError("one short line")
    return text
