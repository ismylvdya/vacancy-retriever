"""Feed groups chosen by the user (15.11.2026): group 6 «Remote in Russia», the order, groups turned off, and the
sources that give only groups that are off."""

from __future__ import annotations

import pytest

from vacancy_retriever.config import AppConfig, SourceConfig
from vacancy_retriever.pipeline.runner import source_wanted
from vacancy_retriever.scoring.engine import ScoringEngine
from vacancy_retriever.scoring.facts import VacancyFacts
from vacancy_retriever.settings.service import SettingsError, SettingsService

from .conftest import NOW
from .test_settings import own_config  # noqa: F401  (fixture: a copy of config/)

REMOTE = {"role_category": "data_analyst", "work_format": "remote", "confidence": "high", "stack_overlap": "strong"}


def all_on(rules):
    """The user's own switches (config/rules.yaml) must not change what these tests check."""
    rules = rules.model_copy(deep=True)
    for info in rules.groups.values():
        info.enabled = True
    return rules


def group(rules, **fields):
    return ScoringEngine(all_on(rules)).evaluate(VacancyFacts(**{**REMOTE, **fields}), None, NOW)


def test_remote_it_in_russia_is_group_six_whoever_the_employer(config):
    assert group(config.rules, country="Russia", employer_type="russian").group == 6
    assert group(config.rules, country="Россия", employer_type="unknown").group == 6
    assert group(config.rules, country="Russia", work_format="unknown").group == 6
    assert group(config.rules, country="unknown", employer_type="russian").group == 3, "country unknown stays in 3"
    assert group(config.rules, country="Kazakhstan", employer_type="intl_russian_speaking").group == 3
    assert group(config.rules, country="Russia", role_category="manual_wood").group == 5, "hands-on is not group 6"


@pytest.mark.parametrize("fields,expected", [
    ({"work_format": "office", "city": "Moscow"}, (7, None)),
    ({"work_format": "hybrid", "city": "Москва"}, (7, None)),
    ({"work_format": "office", "city": "unknown"}, (7, None)),       # the ad names no city: counts as Moscow
    ({"work_format": "office", "city": "Kazan"}, (5, "Office outside Serbia and Moscow")),
    ({"work_format": "remote", "city": "Moscow"}, (6, None)),
    ({"work_format": "office", "city": "Moscow", "role_category": "manual_wood"}, (5, "Office outside Serbia and Moscow")),
])
def test_office_in_moscow_is_group_seven_for_it_only(config, fields, expected):
    verdict = group(config.rules, country="Russia", employer_type="russian", **fields)
    assert (verdict.group, verdict.skip_reason) == expected


def test_a_group_turned_off_skips_its_vacancies(config):
    rules = all_on(config.rules)
    rules.groups[6].enabled = False
    verdict = ScoringEngine(rules).evaluate(VacancyFacts(**REMOTE, country="Russia", employer_type="russian"), None, NOW)
    assert (verdict.decision, verdict.skip_reason, verdict.group) == ("skip", "Group off: Remote in Russia", 6)
    assert ScoringEngine(rules).evaluate(VacancyFacts(**REMOTE, country="unknown", employer_type="russian"),
                                         None, NOW).decision == "show"
    lines = ScoringEngine(rules).explain(VacancyFacts(**REMOTE, country="Russia"), None, NOW)
    assert lines[-1] == "Decision: skip — Group off: Remote in Russia"


def test_order_and_enabled_groups(config):
    rules = all_on(config.rules)
    assert rules.ordered_groups == [7, 6, 1, 3, 2, 4, 5]
    rules.group_order = [3, 9, 3]
    rules.groups[1].enabled = False
    assert rules.ordered_groups == [3, 1, 2, 4, 5, 6, 7], "unknown and repeated numbers dropped, the rest by number"
    assert rules.enabled_groups == [3, 2, 4, 5, 6, 7]


@pytest.mark.parametrize("groups,active,wanted", [
    ([], set(), True),            # nothing known about the source: always collected
    ([1, 2], {6, 3}, False),      # Serbian only, Serbia off
    ([1, 3], {3}, True),          # one of its groups is on
])
def test_a_source_is_collected_while_one_of_its_groups_is_on(groups, active, wanted):
    source = SourceConfig(id="s", type="telegram", name="S", groups=groups)
    assert source_wanted(source, active) is wanted


def test_settings_save_order_switches_and_source_groups(own_config):
    service = SettingsService(own_config)
    source = own_config.sources.sources[0].id
    service.save({f"rules:groups.{n}.enabled": True for n in own_config.rules.groups})
    service.save({"rules:group_order": [1, 2, 3, 4, 5, 6, 7], "rules:groups.2.enabled": False,
                  f"sources:sources[{source}].groups": [3, 1]})
    fresh = AppConfig(own_config.paths)
    assert fresh.rules.ordered_groups == [1, 2, 3, 4, 5, 6, 7] and fresh.rules.enabled_groups == [1, 3, 4, 5, 6, 7]
    assert fresh.sources.sources[0].groups == [1, 3]
    snapshot = SettingsService(fresh).snapshot()
    assert snapshot["group_order"] == [1, 2, 3, 4, 5, 6, 7] and snapshot["groups"][1]["enabled"] is False
    for bad in ({"rules:group_order": [1, 2, 3]}, {"rules:group_order": [1, 1, 2, 3, 4, 5, 6]},
                {f"sources:sources[{source}].groups": [8]}, {"rules:groups.8.enabled": False}):
        with pytest.raises(SettingsError):
            service.save(bad)
