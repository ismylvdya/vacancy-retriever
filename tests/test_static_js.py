"""The interface scripts must at least parse: 18.11.2026 a stray «}» left in core.js stopped every script of the page,
and no Python test noticed. With Node.js installed each file is checked by `node --check`; without it, by brackets."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from .conftest import ROOT

SCRIPTS = sorted((ROOT / "src/vacancy_retriever/web/static").glob("*.js"))


@pytest.mark.parametrize("script", SCRIPTS, ids=[s.name for s in SCRIPTS])
def test_script_parses(script):
    node = shutil.which("node") or next((p for p in ("/opt/homebrew/bin/node", "/usr/local/bin/node")
                                        if shutil.os.path.exists(p)), None)
    if node:
        result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return
    text = script.read_text(encoding="utf-8")
    assert text.count("{") == text.count("}") and text.count("(") == text.count(")")
