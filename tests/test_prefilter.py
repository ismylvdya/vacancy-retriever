from __future__ import annotations

from datetime import timedelta

import pytest

from vacancy_retriever.collectors.telegram import parse_channel_page
from vacancy_retriever.pipeline.prefilter import Prefilter
from vacancy_retriever.storage.items import StoredRawItem

from .conftest import NOW, SAMPLES


def item(title: str = "", text: str = "", multi: bool = False, age_days: float = 1, **meta) -> StoredRawItem:
    return StoredRawItem(1, 1, True, "src", "1", "", title, text or title, None, NOW - timedelta(days=age_days),
                         multi, meta, "pending_llm", None)


@pytest.fixture(scope="module")
def prefilter(config):
    return Prefilter(config.rules)


@pytest.mark.parametrize(("title", "action"), [
    ("Senior Data Scientist", "skip"),
    ("Middle+ Data Analyst", "skip"),
    ("Ведущий аналитик", "skip"),
    ("QA Engineer", "skip"),
    ("1С-программист", "skip"),
    ("Junior Data Scientist", "keep"),
    ("Quantitative Researcher Junior-Middle", "keep"),
    ("Стажёр-аналитик / старший помощник", "keep"),
    ("Data Analyst", "keep"),
    ("JavaScript-free Data Engineer", "keep"),
    ("Junior QA", "skip"),
])
def test_title_rules(prefilter, title, action):
    assert prefilter.check(item(title), NOW).action == action


def test_multi_vacancy_posts_ignore_keywords(prefilter):
    digest = item("Senior Go Developer", "1) Senior Go Developer 2) Junior Data Analyst", multi=True)
    assert prefilter.check(digest, NOW).action == "keep"


def test_old_items_expire(prefilter):
    assert prefilter.check(item("Junior Data Analyst", age_days=31), NOW).reason == "Expired"


def test_rvc_real_posts(prefilter):
    html = (SAMPLES / "html" / "telegram" / "revacancy.html").read_text(encoding="utf-8")
    posts, _ = parse_channel_page(html, "revacancy")
    results = {}
    for post in posts:
        stored = item(post.title, post.text, multi=post.multi, age_days=0, **post.meta)
        results[post.external_id] = prefilter.check(stored, NOW)
    actions = {r.action for r in results.values()}
    assert {"keep", "skip", "hide"} <= actions
    ad = next(r for pid, r in results.items() if "walllet" in next(p.text for p in posts if p.external_id == pid))
    assert ad.action == "hide"
    senior = [r for pid, r in results.items()
              if "| Senior |" in next(p.text for p in posts if p.external_id == pid)]
    assert senior and all(r.action == "skip" and r.reason.startswith("RVC grade") for r in senior)
