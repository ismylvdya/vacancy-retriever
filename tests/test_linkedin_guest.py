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


def make(handler, tmp_path, **settings):
    seen = []

    def recording(request):
        seen.append(request)
        return handler(request)

    fetcher = LinkedInGuestFetcher(GuestSettings(enabled=True, **settings), tmp_path / "linkedin_guest.json",
                                   transport=httpx.MockTransport(recording), sleep=lambda _s: None,
                                   today=lambda: date(2026, 10, 27))
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
    assert len(seen) == 2 and "limit of 2" in limited.alert


def test_linkedin_parser_detail_url_is_the_bare_page():
    from pathlib import Path
    from email import message_from_bytes
    from email.policy import default

    from vacancy_retriever.collectors.email.parsers import PARSERS, load_parsers

    load_parsers()
    sample = Path(__file__).parent.parent / "samples" / "emails" / "linkedin" / "gmail_529.eml"
    vacancies = PARSERS["linkedin"](message_from_bytes(sample.read_bytes(), policy=default))
    assert all(v.detail_url == v.url and v.url.endswith("/") and "?" not in v.url for v in vacancies)
