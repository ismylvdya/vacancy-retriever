"""LinkedIn public job pages: anonymous by construction, stops at the first refusal. No real network here."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from vacancy_retriever.collectors.linkedin_guest import GuestSettings, LinkedInGuestFetcher, job_url, parse_job_page

PAGE = """<html><head><title>Data Scientist - Acme - LinkedIn</title>
<script type="application/ld+json">{"@type": "JobPosting", "description": "<p>From JSON-LD</p>"}</script></head>
<body><h1 class="top-card-layout__title">Junior Data Scientist</h1>
<a class="topcard__org-name-link">Acme d.o.o.</a><span class="topcard__flavor--bullet">Belgrade, Serbia</span>
<div class="show-more-less-html__markup"><p>Python, SQL.</p><ul><li>Serbian is a must</li><li>Hybrid</li></ul></div>
<ul><li class="description__job-criteria-item"><h3>Seniority level</h3><span>Entry level</span></li>
<li class="description__job-criteria-item"><h3>Employment type</h3><span>Full-time</span></li></ul></body></html>"""


def make(handler, tmp_path, tunnel=None, **settings):
    """Page-logic tests run without the VPN requirement; the VPN has its own tests below."""
    settings.setdefault("require_vpn", tunnel is not None)
    seen = []

    def recording(request):
        seen.append(request)
        return handler(request)

    fetcher = LinkedInGuestFetcher(GuestSettings(enabled=True, **settings), tmp_path / "linkedin_guest.json",
                                   transport=httpx.MockTransport(recording), sleep=lambda _s: None,
                                   today=lambda: date(2026, 10, 27), tunnel=tunnel)
    return fetcher, seen


def test_job_url_is_built_from_the_id_only():
    assert job_url("4455684566") == "https://www.linkedin.com/jobs/view/4455684566/"
    with pytest.raises(ValueError):
        job_url("123?otpToken=x")


def test_parse_public_page():
    text = parse_job_page(PAGE)
    assert text.startswith("Junior Data Scientist\nCompany: Acme d.o.o.\nLocation: Belgrade, Serbia")
    assert "Seniority level: Entry level" in text and "Serbian is a must" in text and "Python, SQL." in text
    assert parse_job_page(PAGE.replace("show-more-less-html__markup", "x")).endswith("From JSON-LD")
    assert parse_job_page("<html><body>Sign in to view</body></html>") is None


def test_anonymous_request_no_cookies_no_referer(tmp_path):
    fetcher, seen = make(lambda r: httpx.Response(200, text=PAGE, headers={"set-cookie": "bcookie=abc; Path=/"}),
                         tmp_path)
    assert fetcher.fetch("https://www.linkedin.com/jobs/view/111111/")
    assert fetcher.fetch("https://www.linkedin.com/jobs/view/222222/")
    assert [str(r.url) for r in seen] == ["https://www.linkedin.com/jobs/view/111111/",
                                          "https://www.linkedin.com/jobs/view/222222/"]
    for request in seen:
        assert "cookie" not in request.headers and "referer" not in request.headers
        assert "authorization" not in request.headers


def test_only_bare_job_pages_are_opened(tmp_path):
    fetcher, seen = make(lambda r: httpx.Response(200, text=PAGE), tmp_path)
    for url in ("https://www.linkedin.com/jobs/view/111111/?otpToken=x&midToken=y",
                "https://www.linkedin.com/comm/jobs/view/111111/", "https://www.linkedin.com/in/someone/",
                "https://www.linkedin.com/jobs/search/?keywords=x"):
        assert fetcher.fetch(url) is None
    assert seen == []


@pytest.mark.parametrize("response", [
    httpx.Response(429), httpx.Response(999),
    httpx.Response(302, headers={"location": "https://www.linkedin.com/authwall?trk=x"}),
    httpx.Response(200, text="<html><body><a href='/uas/login'>Sign in to view</a></body></html>"),
])
def test_first_refusal_pauses_until_tomorrow(tmp_path, response):
    fetcher, seen = make(lambda r: response, tmp_path)
    assert fetcher.fetch("https://www.linkedin.com/jobs/view/111111/") is None
    assert fetcher.paused_until() == date(2026, 10, 28) and "paused until 28 Oct" in fetcher.alert
    assert fetcher.fetch("https://www.linkedin.com/jobs/view/222222/") is None
    assert len(seen) == 1, "nothing is retried and no other page is asked for after a refusal"
    again, seen_again = make(lambda r: httpx.Response(200, text=PAGE), tmp_path)
    assert not again.available() and seen_again == [], "the pause survives into the next run of the same day"


def test_redirects_are_never_followed(tmp_path):
    fetcher, seen = make(lambda r: httpx.Response(301, headers={"location": "https://www.linkedin.com/jobs/view/9/"}),
                         tmp_path)
    fetcher.fetch("https://www.linkedin.com/jobs/view/111111/")
    assert len(seen) == 1


def test_disabled_and_limit(tmp_path):
    fetcher = LinkedInGuestFetcher(GuestSettings(enabled=False), tmp_path / "s.json",
                                   transport=httpx.MockTransport(lambda r: pytest.fail("no request when off")))
    assert fetcher.fetch("https://www.linkedin.com/jobs/view/111111/") is None
    limited, seen = make(lambda r: httpx.Response(200, text=PAGE), tmp_path, max_per_run=2)
    for job in ("111111", "222222", "333333"):
        limited.fetch(f"https://www.linkedin.com/jobs/view/{job}/")
    assert len(seen) == 2 and "limit of 2" in limited.note and limited.alert is None


def test_linkedin_parser_detail_url_is_the_bare_page():
    from pathlib import Path
    from email import message_from_bytes
    from email.policy import default

    from vacancy_retriever.collectors.email.parsers import PARSERS, load_parsers

    load_parsers()
    sample = Path(__file__).parent.parent / "samples" / "emails" / "linkedin" / "gmail_529.eml"
    vacancies = PARSERS["linkedin"](message_from_bytes(sample.read_bytes(), policy=default))
    assert all(v.detail_url == v.url and v.url.endswith("/") and "?" not in v.url for v in vacancies)



class FakeTunnel:
    """Stands in for XrayTunnel: records calls, fails when told to."""

    def __init__(self, fail_start=None, fail_verify_after=None):
        self.fail_start, self.fail_verify_after = fail_start, fail_verify_after
        self.calls: list[str] = []
        self.proxy_url = None

    def start(self):
        from vacancy_retriever.collectors.vpn import VpnError

        self.calls.append("start")
        if self.fail_start:
            raise VpnError(self.fail_start)
        # proxy_url stays None here so the mock transport answers; test_pages_go_through_the_tunnel checks the proxy.

    def verify(self):
        from vacancy_retriever.collectors.vpn import VpnError

        self.calls.append("verify")
        if self.fail_verify_after is not None and self.calls.count("verify") > self.fail_verify_after:
            raise VpnError("VPN does not answer")
        return "1.0.0.1"

    def stop(self):
        self.calls.append("stop")


def test_without_a_vpn_linkedin_is_never_opened(tmp_path):
    fetcher, seen = make(lambda r: httpx.Response(200, text=PAGE), tmp_path, require_vpn=True)
    assert fetcher.fetch("https://www.linkedin.com/jobs/view/111111/") is None
    assert seen == [] and "VPN problem" in fetcher.alert


def test_vpn_down_at_start_means_no_linkedin_at_all(tmp_path):
    tunnel = FakeTunnel(fail_start="Xray is not installed")
    fetcher, seen = make(lambda r: httpx.Response(200, text=PAGE), tmp_path, tunnel=tunnel)
    for job in ("111111", "222222"):
        assert fetcher.fetch(f"https://www.linkedin.com/jobs/view/{job}/") is None
    assert seen == [] and tunnel.calls == ["start"], "one failed start, no retries"
    assert fetcher.alert == "LinkedIn not opened — VPN problem: Xray is not installed"
    assert not fetcher.available()


def test_vpn_is_checked_before_every_page_and_a_failure_stops_the_run(tmp_path):
    tunnel = FakeTunnel(fail_verify_after=1)
    fetcher, seen = make(lambda r: httpx.Response(200, text=PAGE), tmp_path, tunnel=tunnel)
    assert fetcher.fetch("https://www.linkedin.com/jobs/view/111111/")   # start
    assert fetcher.fetch("https://www.linkedin.com/jobs/view/222222/")   # verify ok
    assert fetcher.fetch("https://www.linkedin.com/jobs/view/333333/") is None  # verify fails
    assert fetcher.fetch("https://www.linkedin.com/jobs/view/444444/") is None
    assert len(seen) == 2
    assert tunnel.calls == ["start", "verify", "verify", "stop"], "the tunnel is stopped right after the failure"
    assert "VPN does not answer" in fetcher.alert


def test_close_stops_the_tunnel(tmp_path):
    tunnel = FakeTunnel()
    fetcher, _ = make(lambda r: httpx.Response(200, text=PAGE), tmp_path, tunnel=tunnel)
    fetcher.fetch("https://www.linkedin.com/jobs/view/111111/")
    fetcher.close()
    assert tunnel.calls[-1] == "stop"


SUB = "\n".join([
    "vless://11111111-2222-3333-4444-555555555555@nl.example.net:443?type=tcp&security=reality&pbk=PUB&fp=chrome"
    "&sni=www.example.com&sid=ab&flow=xtls-rprx-vision&encryption=none#%F0%9F%87%B3%F0%9F%87%B1%20Netherlands",
    "vless://aaaaaaaa-2222-3333-4444-555555555555@rs.example.net:8443?type=grpc&security=reality&pbk=PUB2&fp=chrome"
    "&sni=www.example.org&sid=cd&serviceName=svc&encryption=none#%F0%9F%87%B7%F0%9F%87%B8%20%D0%A1%D0%B5%D1%80%D0%B1"
    "%D0%B8%D1%8F%20%C2%B7%20%D0%BC%D0%B8%D1%80",
    "trojan://x@y:1#other",
])


def test_subscription_parsing_and_server_choice():
    import base64

    from vacancy_retriever.collectors.vpn import VpnError, choose_server, parse_subscription, xray_config

    servers = parse_subscription(base64.b64encode(SUB.encode()).decode())
    assert [s.name for s in servers] == ["🇳🇱 Netherlands", "🇷🇸 Сербия · мир"]
    serbia = choose_server(servers, "Сербия · мир")
    config = xray_config(serbia, 10808)
    assert config["inbounds"] == [{"listen": "127.0.0.1", "port": 10808, "protocol": "http", "settings": {}}]
    out = config["outbounds"][0]
    assert out["settings"]["vnext"][0]["address"] == "rs.example.net" and out["settings"]["vnext"][0]["port"] == 8443
    assert out["streamSettings"]["network"] == "grpc" and out["streamSettings"]["grpcSettings"]["serviceName"] == "svc"
    assert out["streamSettings"]["realitySettings"]["publicKey"] == "PUB2"
    with pytest.raises(VpnError, match="not in the subscription"):
        choose_server(servers, "Германия")


def test_tunnel_refuses_when_the_exit_ip_is_the_home_ip(monkeypatch):
    from vacancy_retriever.collectors import vpn

    tunnel = vpn.XrayTunnel("https://sub.example", "x", vpn.Path("/tmp"), xray_path="/bin/true")
    tunnel._process = type("P", (), {"poll": lambda self: None})()
    tunnel.proxy_url, tunnel.home_ip = "http://127.0.0.1:1", "9.9.9.9"
    monkeypatch.setattr(vpn.httpx, "Client", lambda **kw: _FakeClient("9.9.9.9"))
    with pytest.raises(vpn.VpnError, match="home IP"):
        tunnel.verify()
    monkeypatch.setattr(vpn.httpx, "Client", lambda **kw: _FakeClient("1.1.1.1"))
    assert tunnel.verify() == "1.1.1.1"
    monkeypatch.setattr(vpn.httpx, "Client", lambda **kw: _FakeClient("8.8.4.4"))
    with pytest.raises(vpn.VpnError, match="changed"):
        tunnel.verify()


class _FakeClient:
    def __init__(self, ip):
        self.ip = ip

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url):
        return type("R", (), {"text": self.ip})()


def test_pages_go_through_the_tunnel_proxy(tmp_path):
    tunnel = FakeTunnel()
    fetcher = LinkedInGuestFetcher(GuestSettings(enabled=True), tmp_path / "s.json", tunnel=tunnel)
    assert fetcher._vpn_ok()
    tunnel.proxy_url = "http://127.0.0.1:10808"
    fetcher._client = None
    client = fetcher._http()
    assert [str(pattern.pattern) for pattern in client._mounts] == ["all://"], "every request uses the tunnel"
    assert client.cookies.jar._policy.allowed_domains() == ()
