"""Surgical edits of the hand-written config files: only the edited value's text changes.

ruamel.yaml finds where a value sits (line, column); the text of that value alone is replaced. Comments, column
alignment of the flow tables (categories, sources) and everything else stay byte for byte — a plain ruamel dump
re-flows the aligned tables. A key missing from a one-line {flow mapping} is appended to it (`, enabled: false`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

PathPart = str | tuple[str, str]  # a mapping key, or (list key, item id) for a list of {id: …} items
_PLAIN = re.compile(r"^[A-Za-z_][\w .+/-]*$")
_RESERVED = {"yes", "no", "true", "false", "on", "off", "null", "y", "n", "~"}


class PatchError(Exception):
    pass


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if _PLAIN.match(text) and text.lower() not in _RESERVED and text == text.strip():
        return text
    return json.dumps(text, ensure_ascii=False)  # a double-quoted JSON string is valid YAML


def _same(stored: Any, wanted: Any) -> bool:
    if isinstance(stored, list) or isinstance(wanted, list):
        return [str(x) for x in stored or []] == [str(x) for x in wanted or []]
    return stored == wanted or (stored is not None and wanted is not None and str(stored) == str(wanted))


def parse_path(dotted: str) -> list[PathPart]:
    """«categories.ds_ml.base», «sources[serbiawork].enabled», «groups.1.title»."""
    parts: list[PathPart] = []
    for piece in dotted.split("."):
        match = re.fullmatch(r"([^\[\]]+)\[([^\[\]]+)\]", piece)
        parts.append((match.group(1), match.group(2)) if match else piece)
    return parts


class YamlDocument:
    def __init__(self, text: str):
        self.text = text
        self._yaml = YAML()
        self._load()

    def _load(self) -> None:
        self.data = self._yaml.load(self.text) or CommentedMap()

    def _lines(self) -> list[str]:
        return self.text.splitlines(keepends=True)

    def _key(self, node: CommentedMap, key: str) -> Any:
        """Keys may be ints in YAML (groups: 1: …) while the path is text."""
        for existing in node:
            if str(existing) == key:
                return existing
        return None

    def _resolve(self, path: Sequence[PathPart]) -> tuple[Any, Any]:
        node: Any = self.data
        for part in path[:-1]:
            node = self._step(node, part)
        last = path[-1]
        if isinstance(last, tuple):
            raise PatchError("a path must end with a key")
        return node, last

    def _step(self, node: Any, part: PathPart) -> Any:
        if isinstance(part, tuple):
            name, item_id = part
            items = node.get(name) if isinstance(node, CommentedMap) else None
            if not isinstance(items, CommentedSeq):
                raise PatchError(f"«{name}» is not a list")
            for item in items:
                if isinstance(item, CommentedMap) and str(item.get("id")) == item_id:
                    return item
            raise PatchError(f"no «{item_id}» in «{name}»")
        key = self._key(node, part) if isinstance(node, CommentedMap) else None
        if key is None:
            raise PatchError(f"«{part}» is not in the file")
        return node[key]

    def get(self, dotted: str) -> Any:
        node, last = self._resolve(parse_path(dotted))
        key = self._key(node, last)
        return None if key is None else node[key]

    def set(self, dotted: str, value: Any) -> None:
        node, last = self._resolve(parse_path(dotted))
        key = self._key(node, last)
        if key is None:
            self._append_to_flow(node, last, value)
        else:
            current = node[key]
            line, col = node.lc.value(key)
            if isinstance(current, CommentedSeq):
                self._replace_list(current, line, col, value)
            elif isinstance(current, CommentedMap):
                raise PatchError(f"«{dotted}» is a section, not a value")
            elif current is None:
                self._replace_empty(*node.lc.key(key), value)  # an empty value has no position of its own
            else:
                self._replace_scalar(line, col, value)
        self._load()
        if not _same(self.get(dotted), value):
            raise PatchError(f"«{dotted}» did not take the new value")

    # ---- text surgery
    def _replace_span(self, line: int, start: int, end: int, new: str) -> None:
        lines = self._lines()
        lines[line] = lines[line][:start] + new + lines[line][end:]
        self.text = "".join(lines)

    def _replace_scalar(self, line: int, col: int, value: Any) -> None:
        text = self._lines()[line]
        self._replace_span(line, col, self._scalar_end(text, col), yaml_scalar(value))

    def _replace_empty(self, line: int, key_col: int, value: Any) -> None:
        """«key:», «key: null», «key: ~» (maybe with a comment after it) → «key: value»."""
        text = self._lines()[line]
        colon = text.index(":", key_col)
        start = colon + 1
        while start < len(text) and text[start] == " ":
            start += 1
        end = self._scalar_end(text, start) if start < len(text) and text[start] not in "#\n" else start
        self._replace_span(line, colon + 1, end, " " + yaml_scalar(value) + (" " if end < len(text.rstrip("\n")) else ""))

    @staticmethod
    def _scalar_end(text: str, col: int) -> int:
        if text[col] in "\"'":
            quote, i = text[col], col + 1
            while i < len(text):
                if text[i] == "\\" and quote == '"':
                    i += 2
                    continue
                if text[i] == quote:
                    return i + 1
                i += 1
            raise PatchError("unterminated quoted value")
        i = col
        while i < len(text) and text[i] not in ",}]\n" and not (text[i] == "#" and text[i - 1] == " "):
            i += 1
        while i > col and text[i - 1] == " ":
            i -= 1
        return i

    def _replace_list(self, current: CommentedSeq, line: int, col: int, value: Any) -> None:
        items = [yaml_scalar(v) for v in value]
        if current.fa.flow_style() or self._lines()[line][col:col + 1] == "[":  # «[]» is not marked as flow
            text = self._lines()[line]
            end = text.index("]", col) + 1
            self._replace_span(line, col, end, "[" + ", ".join(items) + "]")
            return
        if not len(current):
            raise PatchError("an empty block list cannot be edited here")
        first = current.lc.item(0)[0]
        last = current.lc.item(len(current) - 1)[0]
        lines = self._lines()
        indent = lines[first][: len(lines[first]) - len(lines[first].lstrip())]
        lines[first:last + 1] = [f"{indent}- {item}\n" for item in items] or [f"{indent}[]\n"]
        self.text = "".join(lines)

    def _append_to_flow(self, node: CommentedMap, key: str, value: Any) -> None:
        if not node.fa.flow_style():
            raise PatchError(f"«{key}» is not in the file")
        line, col = node.lc.line, node.lc.col
        text = self._lines()[line]
        depth, i = 0, col
        while i < len(text):
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            if depth == 0:
                break
            i += 1
        else:
            raise PatchError("a multi-line flow mapping cannot be edited here")
        text_value = ("[" + ", ".join(yaml_scalar(v) for v in value) + "]") if isinstance(value, list) else yaml_scalar(value)
        self._replace_span(line, i, i, f", {key}: {text_value}")
