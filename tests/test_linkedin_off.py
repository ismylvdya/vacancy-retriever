"""LinkedIn descriptions come only from Google for Jobs: with the real config/mail.yaml nothing ever reaches LinkedIn,
whatever the API answers, and switching the public pages on without an exit does not open them either."""

from __future__ import annotations

import socket

import httpx
import pytest

from vacancy_retriever.collectors import linkedin_guest
from vacancy_retriever.collectors.base import RawItem
from vacancy_retriever.collectors.email.collector import EmailCollector
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.config import AppConfig, SourceConfig
from vacancy_retriever.settings.schema import all_fields
from vacancy_retriever.settings.service import SettingsError, SettingsService

URL = "https://www.linkedin.com/jobs/view/4455684566/"
ITEM = RawItem("gmail", "linkedin:4455684566", URL, "letter", None, title="Data Analyst", detail_url=URL,
               meta={"company": "Acme", "location": "Belgrade", "email": {"parser": "linkedin"}})


@pytest.fixture
def world(tmp_paths, monkeypatch):
    """The real config without API keys of its own; every attempt to go out is written down instead of happening."""
    config = AppConfig(tmp_paths)
    monkeypatch.setattr("vacancy_retriever.collectors.email.collector.AppConfig", lambda: config)
    config.__dict__["secrets"] = type(config.secrets)({"JSEARCH_API_KEY": "k"})
    out: list[str] = []

    def no_dns(host, *args, **kwargs):
        out.append(str(host))
        raise OSError("no network in tests")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    monkeypatch.setattr(linkedin_guest.LinkedInGuestFetcher, "_http", lambda self: out.append("guest client") or 1 / 0)
    return config, out


def collector_with_api(config, answer):
    collector = EmailCollector(SourceConfig(id="gmail", type="email", name="Gmail"), PoliteHttpClient(0, 0))
    search = collector._job_search()
    if search is not None:
        search._transport = httpx.MockTransport(answer)
    return collector


def usage_or(response):
    def answer(request):
        if request.url.path == "/usage":
            return httpx.Response(200, json={"data": {"quotas": [{"name": "Requests", "limit": 200, "remaining": 100}]}})
        return response()
    return answer


NOT_FOUND = usage_or(lambda: httpx.Response(200, json={"status": "OK", "data": {"jobs": []}}))
API_DOWN = usage_or(lambda: httpx.Response(500))


def test_real_config_keeps_the_public_pages_off(world):
    config, _ = world
    assert config.mail.linkedin_descriptions.enabled
    assert config.mail.linkedin_public_pages.enabled is False


@pytest.mark.parametrize("answer", [NOT_FOUND, API_DOWN])
def test_google_miss_is_evaluated_from_the_letter_without_linkedin(world, answer):
    config, out = world
    collector = collector_with_api(config, answer)
    assert collector.fetch_detail(ITEM) is None
    assert collector.defer_detail(ITEM) is None  # no waiting for a page: the vacancy goes on from its letter
    assert collector.fetch_deferred(ITEM, 99) == (None, True)  # even a leftover waiting item: nothing is opened
    collector.finish_details()
    assert out == []


def test_descriptions_off_does_not_fall_back_to_linkedin(world):
    config, out = world
    config.mail.linkedin_descriptions.enabled = False
    collector = collector_with_api(config, NOT_FOUND)
    assert not collector.can_fetch_detail(URL, set())
    assert collector.fetch_deferred(ITEM, 0) == (None, False)
    assert out == []


def test_switched_on_without_an_exit_still_opens_nothing(world):
    config, out = world
    config.mail.linkedin_public_pages.enabled = True
    collector = collector_with_api(config, NOT_FOUND)
    assert collector.defer_detail(ITEM) is None
    text, _ = collector.fetch_deferred(ITEM, 0)
    assert text is None and out == []
    assert "no exit configured" in collector.detail_alert()


def test_settings_screen_cannot_switch_the_public_pages_on(world):
    config, _ = world
    assert not [field for field in all_fields() if "linkedin_public_pages" in field]
    with pytest.raises(SettingsError) as error:
        SettingsService(config).save({"mail:linkedin_public_pages.enabled": True})
    assert "mail:linkedin_public_pages.enabled" in error.value.problems


def test_shared_client_refuses_linkedin():
    with pytest.raises(Exception, match="(?i)linkedin|forbidden"):
        PoliteHttpClient(0, 0).get(URL)
