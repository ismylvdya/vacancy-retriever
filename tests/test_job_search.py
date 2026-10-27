"""LinkedIn descriptions via Google for Jobs (JSearch / SerpApi): only API hosts, exact-vacancy matching, quotas.
No real network: httpx.MockTransport."""

from __future__ import annotations

import json
from datetime import date

import httpx

from vacancy_retriever.collectors.base import RawItem
from vacancy_retriever.collectors.email.collector import EmailCollector
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.collectors.job_search import (
    JobSearchLookup,
    ProviderSettings,
    linkedin_ids,
    same_company,
    same_title,
    simplified_title,
)
from vacancy_retriever.config import AppConfig, SourceConfig

JOB = {"job_id": "abc", "job_title": "Junior Data Analyst", "employer_name": "Acme d.o.o.", "job_city": "Belgrade",
       "job_country": "RS", "job_publisher": "LinkedIn", "job_description": "SQL, Python. Serbian is a plus.",
       "job_apply_link": "https://rs.linkedin.com/jobs/view/junior-data-analyst-at-acme-4455684566?utm_campaign=g",
       "apply_options": [{"publisher": "LinkedIn", "apply_link": "https://rs.linkedin.com/jobs/view/x-4455684566"}]}


def make(handler, tmp_path, providers=None, max_per_run=40, remaining=None):
    """`seen` holds the search requests; JSearch's /usage (free) answers `remaining` and is kept in `usage_calls`."""
    seen, usage_calls = [], []

    def recording(request):
        if request.url.path == "/usage":
            usage_calls.append(request)
            quotas = [] if remaining is None else [{"name": "Requests", "limit": 200, "remaining": remaining}]
            return httpx.Response(200, json={"status": "OK", "data": {"api_id": "jsearch", "quotas": quotas}})
        seen.append(request)
        return handler(request)

    providers = providers or [ProviderSettings("jsearch", "js-key", 50), ProviderSettings("serpapi", "sp-key", 100)]
    lookup = JobSearchLookup(providers, tmp_path / "job_search.json", max_per_run,
                             transport=httpx.MockTransport(recording), today=lambda: date(2026, 10, 27))
    lookup.usage_calls = usage_calls
    return lookup, seen


def jsearch(*jobs):
    return lambda request: httpx.Response(200, json={"status": "OK", "data": {"jobs": list(jobs), "cursor": "c"}})


def test_matching_helpers():
    assert same_company("Acme d.o.o.", "ACME") and same_company("Nordeus", "Nordeus Ltd")
    assert not same_company("Acme", "Globex")
    assert same_title("Junior Data Analyst", "Junior Data Analyst (m/f)")
    assert not same_title("Junior Data Analyst", "Senior Data Engineer")
    assert simplified_title("Data Scientist (Computer Vision)") == "Data Scientist"
    assert simplified_title("Data Services Analyst (Hybrid/Remote)") == "Data Services Analyst"
    assert simplified_title("Analyst – Finance | Remote") == "Analyst"
    assert simplified_title("Junior Data Analyst") == "Junior Data Analyst"
    assert linkedin_ids(["https://rs.linkedin.com/jobs/view/python-3-dev-at-x-4455684566?u=1",
                         "https://www.linkedin.com/jobs/view/123456789/"]) == {"4455684566", "123456789"}


def test_found_by_linkedin_number_only_api_host_is_requested(tmp_path):
    lookup, seen = make(jsearch({**JOB, "job_title": "Data Analyst — Junior", "employer_name": "Acme Group"}), tmp_path)
    text = lookup.lookup("Junior Data Analyst", "Acme", "Belgrade, Serbia (Hybrid)", "4455684566")
    assert text.startswith("Data Analyst — Junior\nCompany: Acme Group\nLocation: Belgrade, RS")
    assert "Google for Jobs (via LinkedIn)" in text and "Serbian is a plus." in text
    assert [str(r.url).split("?")[0] for r in seen] == ["https://api.openwebninja.com/jsearch/search-v2"]
    assert seen[0].headers["x-api-key"] == "js-key"
    assert seen[0].url.params["query"] == "Junior Data Analyst Acme Belgrade" and seen[0].url.params["country"] == "rs"
    assert lookup.note == "LinkedIn descriptions: found 1, not found 0 (Google for Jobs)"


def test_half_an_emoji_does_not_break_storing(tmp_path):
    body = json.dumps({"data": {"jobs": [{**JOB, "job_description": "Join us \ud83c today"}]}})  # "\\ud83c" on the wire
    lookup, _ = make(lambda r: httpx.Response(200, content=body.encode()), tmp_path)
    text = lookup.lookup("Junior Data Analyst", "Acme", "Belgrade", "4455684566")
    assert text.endswith("Join us ? today") and text.encode("utf-8")


def test_other_linkedin_number_is_another_vacancy(tmp_path):
    other = {**JOB, "job_apply_link": "https://www.linkedin.com/jobs/view/999999999/", "apply_options": []}
    lookup, _ = make(jsearch(other), tmp_path)
    assert lookup.lookup("Junior Data Analyst", "Acme", "Belgrade", "4455684566") is None


def test_without_a_linkedin_link_company_and_title_must_match(tmp_path):
    ats = {**JOB, "job_apply_link": "https://jobs.lever.co/acme/1", "apply_options": [], "job_publisher": "Lever"}
    lookup, seen = make(jsearch({**ats, "employer_name": "Globex"}, {**ats, "job_title": "Senior Data Engineer"}, ats),
                        tmp_path)
    assert "via Lever" in lookup.lookup("Junior Data Analyst", "Acme", "Remote", "4455684566")
    assert "country" not in seen[0].url.params  # not Serbia: Google's default
    lookup, _ = make(jsearch({**ats, "employer_name": "Globex"}), tmp_path)
    assert lookup.lookup("Junior Data Analyst", "Acme", "Remote", "4455684566") is None


def test_honest_not_found_does_not_spend_the_second_provider(tmp_path):
    lookup, seen = make(jsearch(), tmp_path)
    assert lookup.lookup("Junior Data Analyst", "Acme", "Belgrade", "4455684566") is None
    assert len(seen) == 1 and lookup.not_found == 1


def test_second_query_without_the_title_tail(tmp_path):
    answers = [{"status": "OK", "data": {"jobs": []}}, {"status": "OK", "data": {"jobs": [JOB]}}]
    lookup, seen = make(lambda r: httpx.Response(200, json=answers[len(seen) - 1]), tmp_path)
    assert lookup.lookup("Junior Data Analyst (Hybrid/Remote)", "Acme", "Belgrade City (Hybrid)", "4455684566")
    assert [r.url.params["query"] for r in seen] == ["Junior Data Analyst (Hybrid/Remote) Acme Belgrade",
                                                     "Junior Data Analyst Acme Belgrade"]
    lookup, seen = make(jsearch(), tmp_path)  # a plain title: no second query
    lookup.lookup("Junior Data Analyst", "Acme", "Belgrade")
    assert len(seen) == 1


def test_remaining_quota_comes_from_the_provider(tmp_path):
    lookup, seen = make(jsearch(JOB), tmp_path, [ProviderSettings("jsearch", "js-key", 200)], remaining=1)
    assert lookup.lookup("Junior Data Analyst", "Acme", "Belgrade", "4455684566")
    assert not lookup.available() and lookup.lookup("Junior Data Analyst", "Acme", "Belgrade") is None
    assert len(seen) == 1 and len(lookup.usage_calls) == 1  # asked once per run
    assert lookup.usage_calls[0].headers["x-api-key"] == "js-key"
    lookup, seen = make(jsearch(JOB), tmp_path, [ProviderSettings("jsearch", "js-key", 200)], remaining=0)
    assert not lookup.available() and seen == []


def test_refused_key_stops_the_provider_and_serpapi_answers(tmp_path):
    def handler(request):
        if request.url.host == "api.openwebninja.com":
            return httpx.Response(403, json={"message": "bad key"})
        return httpx.Response(200, json={"jobs_results": [
            {"title": "Junior Data Analyst", "company_name": "Acme", "location": "Belgrade, Serbia", "via": "LinkedIn",
             "description": "From SerpApi", "share_link": "https://www.google.com/search?q=x",
             "apply_options": [{"title": "LinkedIn", "link": "https://rs.linkedin.com/jobs/view/a-4455684566"}]}]})

    lookup, seen = make(handler, tmp_path)
    assert lookup.lookup("Junior Data Analyst", "Acme", "Belgrade", "4455684566").endswith("From SerpApi")
    assert lookup.alert == "LinkedIn descriptions: jsearch — HTTP 403 (API key refused)"
    assert "sp-key" not in lookup.alert
    assert seen[1].url.params["engine"] == "google_jobs" and seen[1].url.params["gl"] == "rs"
    lookup.lookup("Junior Data Analyst", "Acme", "Belgrade", "4455684566")
    assert [r.url.host for r in seen] == ["api.openwebninja.com", "serpapi.com", "serpapi.com"]  # jsearch not retried


def test_monthly_free_limit_is_kept_across_runs(tmp_path):
    providers = [ProviderSettings("jsearch", "js-key", 2)]
    lookup, seen = make(jsearch(), tmp_path, providers)
    for _ in range(3):
        lookup.lookup("Junior Data Analyst", "Acme", "Belgrade")
    assert len(seen) == 2 and "monthly free limits" in lookup.note
    usage = json.loads((tmp_path / "job_search.json").read_text())
    assert usage == {"2026-10": {"jsearch": 2}}
    again, seen = make(jsearch(), tmp_path, providers)  # the next run, same month
    assert not again.available() and seen == []


def test_per_run_limit_and_missing_keys(tmp_path):
    lookup, seen = make(jsearch(), tmp_path, max_per_run=1)
    lookup.lookup("A title", "Acme")
    assert not lookup.available() and "limit of 1 lookups per run" in lookup.note and len(seen) == 1
    lookup, seen = make(jsearch(), tmp_path, [ProviderSettings("jsearch", None, 50)])
    assert not lookup.available() and "no API key (jsearch" in lookup.note and lookup.alert is None


def test_network_error_is_not_fatal_and_hides_the_url(tmp_path, caplog):
    def handler(request):
        raise httpx.ConnectError(f"boom {request.url}")

    lookup, _ = make(handler, tmp_path)
    assert lookup.lookup("Junior Data Analyst", "Acme") is None
    assert "sp-key" not in caplog.text and "js-key" not in caplog.text


def test_email_collector_uses_job_search_not_linkedin(tmp_paths, monkeypatch):
    config = AppConfig(tmp_paths)
    monkeypatch.setattr("vacancy_retriever.collectors.email.collector.AppConfig", lambda: config)
    config.__dict__["secrets"] = type(config.secrets)({"JSEARCH_API_KEY": "js-key"})
    assert config.mail.linkedin_descriptions.enabled and not config.mail.linkedin_public_pages.enabled
    collector = EmailCollector(SourceConfig(id="gmail", type="email", name="Gmail"), PoliteHttpClient(0, 0))
    seen = []
    collector._job_search()._transport = httpx.MockTransport(lambda r: seen.append(r) or jsearch(JOB)(r))
    url = "https://www.linkedin.com/jobs/view/4455684566/"
    item = RawItem("gmail", "linkedin:4455684566", url, "letter", None, title="Junior Data Analyst", detail_url=url,
                   meta={"company": "Acme", "location": "Belgrade, Serbia (Hybrid)", "email": {"parser": "linkedin"}})
    assert collector.can_fetch_detail(url, set())
    assert "Serbian is a plus." in collector.fetch_detail(item)
    assert [str(r.url).split("?")[0] for r in seen] == ["https://api.openwebninja.com/usage",
                                                        "https://api.openwebninja.com/jsearch/search-v2"]
    assert collector._guest is None  # the public-pages fetcher was never even built
    collector.finish_details()
    assert "found 1" in collector.detail_note()
