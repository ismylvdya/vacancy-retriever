"""Statistics → Runs: MB the app downloaded during a run."""

import httpx

from vacancy_retriever import traffic
from vacancy_retriever.collectors.http import PoliteHttpClient


def test_shared_http_client_counts_downloaded_bytes():
    client = PoliteHttpClient(0, 0, transport=httpx.MockTransport(lambda r: httpx.Response(200, text="x" * 1000)))
    before = traffic.total()
    client.get_text("https://example.com/jobs")
    assert 1000 <= traffic.total() - before < 1300  # body + headers
