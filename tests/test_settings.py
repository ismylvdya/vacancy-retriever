"""Settings screen: values are written back value by value; the rest of the hand-written file stays byte for byte."""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from vacancy_retriever.config import AppConfig
from vacancy_retriever.paths import ProjectPaths
from vacancy_retriever.settings.schema import all_fields
from vacancy_retriever.settings.service import SettingsError, SettingsService
from vacancy_retriever.settings.yaml_patch import PatchError, YamlDocument
from vacancy_retriever.storage import Storage
from vacancy_retriever.web.app import create_app

from .conftest import ROOT

PORT = 18765
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}
TEXT = """# comment on top
penalties:
  stack_partial: 1      # inline comment
  max_stars:
categories:
  ds_ml:                 {label: "Data Science / ML",        kind: it,     base: 5, cap: 5}
  qa:                    {label: "QA",                       kind: drop,   drop_reason: "QA / testing"}
grades: [middle, senior]
words:
  - qa
  - "c#"
sources:
  - {id: one,   type: telegram, name: "One"}
  - {id: two,   type: telegram, name: "Two", enabled: false}
groups:
  1: {title: "IT", note: "With permit"}
"""


def changed_lines(before: str, after: str) -> list[tuple[str, str]]:
    return [(a, b) for a, b in zip(before.splitlines(), after.splitlines(), strict=False) if a != b]


def test_scalar_edits_keep_comments_and_alignment():
    doc = YamlDocument(TEXT)
    doc.set("penalties.stack_partial", 2)
    doc.set("categories.ds_ml.base", 4)
    doc.set("categories.qa.drop_reason", "QA, testing")
    doc.set("groups.1.title", "IT & data")
    assert changed_lines(TEXT, doc.text) == [
        ("  stack_partial: 1      # inline comment", "  stack_partial: 2      # inline comment"),
        ('  ds_ml:                 {label: "Data Science / ML",        kind: it,     base: 5, cap: 5}',
         '  ds_ml:                 {label: "Data Science / ML",        kind: it,     base: 4, cap: 5}'),
        ('  qa:                    {label: "QA",                       kind: drop,   drop_reason: "QA / testing"}',
         '  qa:                    {label: "QA",                       kind: drop,   drop_reason: "QA, testing"}'),
        ('  1: {title: "IT", note: "With permit"}', '  1: {title: "IT & data", note: "With permit"}'),
    ]


def test_empty_value_lists_and_missing_keys():
    doc = YamlDocument(TEXT)
    doc.set("penalties.max_stars", 3)
    doc.set("grades", ["senior", "lead"])
    doc.set("words", ["qa", "c#", "1с"])
    doc.set("sources[one].enabled", False)
    doc.set("sources[two].enabled", True)
    doc.set("categories.qa.base", 3)  # a skipped category becomes a scored one: base / cap are added
    assert "  max_stars: 3\n" in doc.text and "grades: [senior, lead]\n" in doc.text
    assert '  - qa\n  - "c#"\n  - "1с"\n' in doc.text
    assert '{id: one,   type: telegram, name: "One", enabled: false}' in doc.text
    assert '{id: two,   type: telegram, name: "Two", enabled: true}' in doc.text
    assert 'drop_reason: "QA / testing", base: 3}' in doc.text
    assert doc.text.startswith("# comment on top\n")


def test_unknown_paths_are_refused():
    doc = YamlDocument(TEXT)
    for path in ("nope.value", "sources[three].enabled", "penalties.missing", "categories.ds_ml"):
        with pytest.raises(PatchError):
            doc.set(path, 1)


@pytest.fixture
def own_config(tmp_path):
    """A copy of config/: these tests write it."""
    shutil.copytree(ROOT / "config", tmp_path / "config", ignore=shutil.ignore_patterns(".env"))
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    return AppConfig(paths)


def test_every_field_of_the_screen_exists_in_the_real_config(own_config):
    snapshot = SettingsService(own_config).snapshot()
    missing = [field_id for field_id in all_fields() if snapshot["values"][field_id] is None
               and not all_fields()[field_id].nullable and all_fields()[field_id].kind != "stars"]
    assert missing == []
    assert snapshot["fit"]["ds_ml"]["rows"][0]["cells"][0]["stars"] == 5


def test_save_validates_everything_before_writing(own_config):
    service = SettingsService(own_config)
    rules = own_config.paths.config / "rules.yaml"
    before = rules.read_text(encoding="utf-8")
    with pytest.raises(SettingsError) as error:
        service.save({"rules:penalties.stack_partial": 2, "rules:categories.qa.base": 9,
                      "mail:linkedin_public_pages.max_delay": "abc", "rules:nonsense": 1})
    assert set(error.value.problems) == {"rules:categories.qa.base", "mail:linkedin_public_pages.max_delay",
                                         "rules:nonsense"}
    assert rules.read_text(encoding="utf-8") == before, "nothing is written when anything is wrong"
    result = service.save({"rules:penalties.stack_partial": 2, "sources:sources[serbiawork].enabled": False,
                           "rules:dropped_seniority": ["senior", "lead"]})
    assert result.files == ["rules", "sources"] and result.rules_changed
    assert own_config.rules.penalties.stack_partial == 2, "the cached config is refreshed"
    assert not own_config.source("serbiawork").enabled
    assert len(changed_lines(before, rules.read_text(encoding="utf-8"))) == 2


def test_preview_changes_the_matrix_without_writing(own_config):
    service = SettingsService(own_config)
    rules = own_config.paths.config / "rules.yaml"
    before = rules.read_text(encoding="utf-8")
    fit = service.preview_fit({"rules:penalties.stack_partial": 3})
    partly = next(r for r in fit["ds_ml"]["rows"] if r["stack"] == "partial")
    assert partly["cells"][0]["stars"] == 2 and "−3 partial stack" in partly["cells"][0]["why"]
    assert rules.read_text(encoding="utf-8") == before


def test_settings_api_saves_and_recalculates(own_config):
    storage = Storage.open(own_config.paths.default_db)
    app = create_app(own_config, storage, port=PORT)
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        assert client.get("/api/settings").json()["sections"][0]["id"] == "fit"
        bad = client.post("/api/settings", json={"changes": {"rules:max_age_days": 0}}, headers=ORIGIN)
        assert bad.status_code == 422 and "rules:max_age_days" in bad.json()["problems"]
        good = client.post("/api/settings", json={"changes": {"rules:max_age_days": 21}}, headers=ORIGIN)
        assert good.status_code == 200 and good.json()["recalculated"] == {"total": 0, "changed": 0}
        assert good.json()["settings"]["values"]["rules:max_age_days"] == 21
    storage.db.close()
