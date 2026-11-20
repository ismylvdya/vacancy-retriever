"""LinkedIn is contacted only when the user switched its pages on AND a private exit works right now. After install it
is off; the real config/mail.yaml reaches nothing whatever the API answers. No real network here."""

from __future__ import annotations

import shutil
import socket
from pathlib import Path

import httpx
import pytest

from vacancy_retriever.collectors import linkedin_guest
from vacancy_retriever.collectors.base import RawItem
from vacancy_retriever.collectors.egress import Egress, EgressError, ExitGuard, HomeAddresses, ProxyEgress, public_ip
from vacancy_retriever.collectors.email.collector import EmailCollector
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.linkedin_guest import GuestBlocked, GuestSettings, LinkedInGuestFetcher
from vacancy_retriever.config import AppConfig, LinkedInPublicPages, MailConfig, ProjectPaths, SourceConfig
from vacancy_retriever.settings.schema import all_fields
from vacancy_retriever.settings.service import LINKEDIN_CONFIRM, LinkedInSwitchError, SettingsError, SettingsService

ROOT = Path(__file__).parent.parent
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


def test_after_install_only_google_for_jobs_is_on(world):
    config, _ = world
    assert config.mail.linkedin_descriptions.enabled
    assert config.mail.linkedin_public_pages.enabled is False
    assert LinkedInPublicPages().enabled is False and MailConfig().linkedin_public_pages.enabled is False
    assert GuestSettings().enabled is False


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
    assert collector.fetch_deferred(ITEM, 0) == (None, True)
    assert out == []


def test_switched_on_without_an_exit_still_opens_nothing(world):
    config, out = world
    config.mail.linkedin_public_pages.enabled = True
    collector = collector_with_api(config, NOT_FOUND)
    assert collector.defer_detail(ITEM) is None
    text, _ = collector.fetch_deferred(ITEM, 0)
    assert text is None and out == []
    assert "no exit configured" in collector.detail_alert()


class WorkingExit(Egress):
    proxy_url = None
    label = "proxy test.example"

    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    def start(self):
        self.calls.append("start")
        if self.fail:
            raise EgressError(self.fail)
        self.proxy_url = "http://user:secret@test.example:8080"

    def verify(self):
        self.calls.append("verify")
        if self.fail:
            raise EgressError(self.fail)

    def stop(self):
        self.calls.append("stop")
        self.proxy_url = None


JOB = "https://www.linkedin.com/jobs/view/111111/"


def guest(tmp_path, transport, egress, **kwargs):
    return LinkedInGuestFetcher(GuestSettings(enabled=True, min_delay=1, max_delay=1), tmp_path / "s.json",
                                transport=transport, egress=egress, sleep=lambda _s: None, **kwargs)


def never(_request):
    pytest.fail("a request left for LinkedIn")


def test_the_client_refuses_to_exist_without_a_started_exit(tmp_path):
    transport = httpx.MockTransport(never)
    with pytest.raises(GuestBlocked):
        guest(tmp_path, transport, None)._http()
    with pytest.raises(GuestBlocked):
        guest(tmp_path, transport, WorkingExit())._http()  # configured, not started


@pytest.mark.parametrize("egress", [None, WorkingExit(fail="the proxy does not answer")])
def test_no_working_exit_means_no_request(tmp_path, egress):
    fetcher = guest(tmp_path, httpx.MockTransport(never), egress)
    assert fetcher.fetch(JOB) is None
    assert "exit problem" in fetcher.alert and not fetcher.available()


def test_an_exit_failing_later_stops_everything(tmp_path):
    seen = []
    egress = WorkingExit()
    fetcher = guest(tmp_path, httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(404)), egress)
    fetcher.fetch(JOB)
    egress.fail = "the exit address changed"
    assert fetcher.fetch("https://www.linkedin.com/jobs/view/222222/") is None
    assert len(seen) == 1 and egress.calls[-1] == "stop"


def test_the_real_transport_goes_only_through_the_proxy(tmp_path, monkeypatch):
    """Not the injected test transport: what production builds. System and environment proxies are ignored."""
    built = {}
    real = httpx.HTTPTransport

    def spy(*args, **kwargs):
        built.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "HTTPTransport", spy)
    monkeypatch.setenv("HTTPS_PROXY", "http://elsewhere.example:1")
    fetcher = guest(tmp_path, None, WorkingExit())
    fetcher._egress.start()
    fetcher._egress_up = True
    client = fetcher._http()
    assert built["proxy"] == "http://user:secret@test.example:8080" and built["trust_env"] is False
    assert client.trust_env is False and client.follow_redirects is False
    client.close()


def test_a_home_address_through_the_proxy_is_refused(monkeypatch):
    """The proxy lets traffic out from the home network: the same as no proxy."""
    guard = ExitGuard()
    monkeypatch.setattr(guard, "_echo", lambda url, proxy, required: public_ip("8.8.8.8") if "api6" not in url else None)
    exit_ = ProxyEgress("http://user:secret@test.example:8080", guard)
    with pytest.raises(EgressError, match="home network"):
        exit_.start()
    assert exit_.proxy_url is None
    assert isinstance(guard.home, HomeAddresses)


def test_switching_off_stops_a_run_that_is_already_going(tmp_path):
    seen, switch = [], {"on": True}
    fetcher = guest(tmp_path, httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(404)), WorkingExit(),
                    still_enabled=lambda: switch["on"])
    fetcher.fetch(JOB)
    switch["on"] = False
    assert fetcher.fetch("https://www.linkedin.com/jobs/view/222222/") is None and len(seen) == 1


# ---------- the switch of the settings screen


@pytest.fixture
def own_config(tmp_path):
    shutil.copytree(ROOT / "config", tmp_path / "config", ignore=shutil.ignore_patterns(".env"))
    paths = ProjectPaths(tmp_path)
    paths.ensure()
    return AppConfig(paths)


def switch_in_file(config):
    return AppConfig(config.paths).mail.linkedin_public_pages.enabled


def test_save_button_cannot_reach_the_switch(own_config):
    assert "mail:linkedin_public_pages.enabled" not in all_fields()
    assert "mail:linkedin_public_pages.exit.proxy_env" not in all_fields()
    service = SettingsService(own_config, lambda _c: WorkingExit())
    with pytest.raises(SettingsError) as error:
        service.save({"mail:linkedin_public_pages.enabled": True})
    assert "mail:linkedin_public_pages.enabled" in error.value.problems and not switch_in_file(own_config)


def test_turning_on_needs_the_exact_phrase(own_config):
    exit_ = WorkingExit()
    service = SettingsService(own_config, lambda _c: exit_)
    for phrase in ("", "enable linkedin", "yes", LINKEDIN_CONFIRM + " "):
        with pytest.raises(LinkedInSwitchError):
            service.linkedin_pages_on(phrase)
    assert exit_.calls == [] and not switch_in_file(own_config)


def test_turning_on_needs_a_proxy(own_config):
    service = SettingsService(own_config)  # the real factory, config/.env has no proxy
    assert service.linkedin_pages() == {"enabled": False, "exit": "", "confirm": LINKEDIN_CONFIRM}
    with pytest.raises(LinkedInSwitchError, match="No proxy"):
        service.linkedin_pages_on(LINKEDIN_CONFIRM)
    assert not switch_in_file(own_config)


def test_turning_on_needs_a_proxy_that_works(own_config):
    exit_ = WorkingExit(fail="the proxy does not answer")
    with pytest.raises(LinkedInSwitchError, match="does not answer"):
        SettingsService(own_config, lambda _c: exit_).linkedin_pages_on(LINKEDIN_CONFIRM)
    assert not switch_in_file(own_config) and exit_.calls == ["start", "stop"]


def test_on_with_a_working_proxy_and_off_always(own_config):
    exit_ = WorkingExit()
    service = SettingsService(own_config, lambda _c: exit_)
    service.linkedin_pages_on(LINKEDIN_CONFIRM)
    assert switch_in_file(own_config) and exit_.calls == ["start", "stop"] and exit_.proxy_url is None
    assert "secret" not in str(service.snapshot()["linkedin_pages"])
    calls = len(exit_.calls)
    SettingsService(own_config).linkedin_pages_off()  # no exit at all, no network
    assert not switch_in_file(own_config) and len(exit_.calls) == calls
    assert "# ВЫКЛЮЧЕНО" in (own_config.paths.config / "mail.yaml").read_text(encoding="utf-8"), "comments stay"


def test_shared_client_refuses_linkedin():
    with pytest.raises(Exception, match="(?i)linkedin|forbidden|never-open"):
        PoliteHttpClient(0, 0).get(URL)


def test_only_the_guest_module_knows_how_to_reach_linkedin():
    """The audit as a test: outside it, no code builds a request to LinkedIn on its own."""
    source = ROOT / "src" / "vacancy_retriever"
    clients = [p.relative_to(source).as_posix() for p in source.rglob("*.py") if "linkedin_guest" not in p.name
               and "httpx.Client(" in p.read_text(encoding="utf-8")]
    # accreditation/dadata.py posts only to suggestions.dadata.ru (SUGGEST_URL), a fixed address.
    assert sorted(clients) == ["accreditation/dadata.py", "apply/drive.py", "collectors/egress.py", "collectors/http.py", "collectors/job_search.py",
                               "collectors/vpn.py"]
