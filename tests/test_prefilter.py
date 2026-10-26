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


@pytest.mark.parametrize(("text", "languages", "action"), [
    ("🟩 Junior Data Analyst | Junior ▫️ Acme ▫️ Germany | Berlin", ["en"], "skip"),
    ("🟩 Junior Data Analyst | Junior ▫️ Acme ▫️ Serbia | Belgrade", ["en"], "keep"),
    ("🟩 Junior Data Analyst | Junior ▫️ Acme ▫️ Germany | Berlin", ["en", "ru"], "keep"),
    ("🟩 Junior Data Analyst | Junior ▫️ Acme ▫️ Germany | Berlin", None, "keep"),
])
def test_rvc_foreign_rule(prefilter, text, languages, action):
    meta = {"rvc": True, "grade_emoji": "🟩", "grade": "junior"}
    if languages is not None:
        meta["required_languages"] = languages
    result = prefilter.check(item("Junior Data Analyst", text, **meta), NOW)
    assert result.action == action
    if action == "skip":
        assert result.reason == "RVC: no Russian, not Serbia"


def test_language_filter_on_real_serbian_ad(prefilter):
    from vacancy_retriever.collectors.sites.infostud import parse_detail
    text = parse_detail((SAMPLES / "html" / "infostud" / "detail.html").read_text(encoding="utf-8"))
    assert prefilter.check_language(item("AI Developer"), text).reason == "Ad in Serbian, Russian not required"
    assert prefilter.check_language(item("AI Developer"), text + "\nPoznavanje ruskog jezika").action == "keep"


@pytest.mark.parametrize(("text", "serbian"), [
    ("We are looking for a Junior Data Analyst to join our team in Belgrade. Experience with SQL is a plus.", False),
    ("Tražimo kandidata za rad u magacinu. Potrebno je iskustvo i poznavanje rada na računaru, nudimo uslove.", True),
    ("Ищем аналитика данных в команду, удалённо, опыт работы с SQL и Python.", False),
    ("Тражимо кандидата за рад у магацину, потребно је искуство.", True),
])
def test_looks_serbian(text, serbian):
    from vacancy_retriever.pipeline.prefilter import looks_serbian
    assert looks_serbian(text) is serbian


def test_grade_from_site_data_is_dropped_for_free(config):
    from datetime import UTC, datetime

    from vacancy_retriever.pipeline.prefilter import Prefilter
    from vacancy_retriever.storage.items import StoredRawItem

    def item(grade):
        return StoredRawItem(1, 1, False, "habr", "1", "", "Data Analyst", "Data Analyst", None, None, False,
                             {"grade": grade}, "pending_llm", None)

    prefilter = Prefilter(config.rules)
    assert prefilter.check(item("Senior"), datetime.now(UTC)).reason == "Grade: senior"
    assert prefilter.check(item(None), datetime.now(UTC)).action == "keep"
    assert prefilter.check(item("Junior"), datetime.now(UTC)).action == "keep"
