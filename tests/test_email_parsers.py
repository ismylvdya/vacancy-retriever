"""Precise e-mail parsers: helpers, detail pages and how the collector treats each parser answer."""

from __future__ import annotations

from email.message import EmailMessage

import httpx

from vacancy_retriever.collectors.email.collector import EmailCollector, items_from_message
from vacancy_retriever.collectors.email.parsers import DETAIL_PARSERS, PARSERS, load_parsers, office_abroad
from vacancy_retriever.collectors.email.parsers.changellenge import real_url
from vacancy_retriever.collectors.email.parsers.linkedin import location_hints
from vacancy_retriever.collectors.email.parsers.superjob import real_path
from vacancy_retriever.collectors.hh_descriptions import HhDescriptions
from vacancy_retriever.collectors.http import PoliteHttpClient
from vacancy_retriever.config import SenderRule, SourceConfig

from .conftest import SAMPLES

DETAILS = SAMPLES / "html" / "email_details"


def letter(sender: str, html: str, subject: str = "Вакансии", bulk: bool = True) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["Subject"] = subject
    message["Message-ID"] = "<m1@example.com>"
    message["Date"] = "Mon, 26 Oct 2026 09:00:00 +0300"
    message.set_content("plain")
    message.add_alternative(html + ("<p>Отписаться</p>" if bulk else ""), subtype="html")
    return message


def test_linkedin_location_hints():
    assert location_hints("Belgrade (Hybrid)") == {"work_format": "hybrid", "country": "Serbia", "city": "Belgrade"}
    assert location_hints("Serbia (Remote)") == {"work_format": "remote", "country": "Serbia"}
    assert location_hints("Novi Sad") == {"country": "Serbia", "city": "Novi Sad"}
    assert location_hints("Berlin (On-site)") == {"work_format": "office"}


def test_office_abroad():
    assert office_abroad("Казань", "Комбинированный")
    assert office_abroad("Санкт-Петербург", "")
    assert not office_abroad("Москва", "Комбинированный"), "office in Moscow is welcome since 16.11.2026"
    assert not office_abroad("Москва", "Удалённо")
    assert not office_abroad("Любой", "Комбинированный")
    assert not office_abroad("Белград", "Офис")
    assert not office_abroad("", "Офис")


def test_superjob_links_lose_the_login_token():
    assert real_path("https://www.superjob.ru/ml/r/L3N1cGVyLXJlc3VtZSNidXk/?i=6a9e533cb07dc7.78647427") == "/super-resume#buy"


def test_changellenge_tracker_links_are_decoded():
    href = "https://tn-hls.mckx.ru/c/X/Y/Z/?u=https%3A%2F%2Fchangellenge.com%2Fvacancy%2Fintern-ai-psb-moscow%2F"
    assert real_url(href) == "https://changellenge.com/vacancy/intern-ai-psb-moscow/"


def test_vk_detail_keeps_moscow_office_and_drops_closed():
    load_parsers()
    office = DETAIL_PARSERS["vk_internship"]((DETAILS / "vk_office_moscow.html").read_text(encoding="utf-8"))
    assert office.skip_reason is None and "Город:" in office.text and "*" not in office.text
    closed = DETAIL_PARSERS["vk_internship"]((DETAILS / "vk_closed_remote.html").read_text(encoding="utf-8"))
    assert closed.skip_reason == "Recruitment closed"


def test_changellenge_detail_moscow_office():
    load_parsers()
    result = DETAIL_PARSERS["changellenge"]((DETAILS / "changellenge_moscow.html").read_text(encoding="utf-8"))
    assert result.skip_reason is None and "Компания: ПСБ" in result.text, "office in Moscow is welcome since 16.11.2026"


def test_hh_links_keep_only_the_vacancy_id():
    load_parsers()
    html = ("<table><tr><td><a href='https://hh.ru/vacancy/123&loginkey=SECRET&email_hash=x'>Data Analyst</a>"
            "<p>от</p><p>120000</p><p>₽ за месяц</p><p>X5 Tech</p><a href='https://hh.ru/vacancy/123'>Посмотреть вакансию</a>"
            "</td></tr></table>")
    [vacancy] = PARSERS["hh_ru"](letter("hh.ru <noreply@hh.ru>", html + "<p>Удалённая работа</p>", "Новые вакансии"))
    assert vacancy.url == "https://hh.ru/vacancy/123" and vacancy.key == "hh:123"
    assert vacancy.detail_url == "https://get-works.ru/job-detail.php?vac=123"
    assert vacancy.company == "X5 Tech"
    assert vacancy.extra == {"template": "hh_saved_search", "salary": "от 120000 ₽ за месяц", "work_format": "remote"}
    assert "SECRET" not in vacancy.text


def test_hh_email_enriches_the_exact_vacancy_without_opening_hh(config, monkeypatch):
    load_parsers()
    seen = []

    def answer(request):
        seen.append(str(request.url))
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        return httpx.Response(200, text='''
            <div class="row-bottom">
                <h2 class="detail-title">Описание вакансии</h2>
                <div class="p-otstup"><p><strong>Обязанности:</strong></p><ul><li>Поддерживать отчёты</li></ul></div>
                <a href="https://hh.ru/vacancy/123">Посмотреть контакты работодателя</a>
            </div><h2>Похожие вакансии</h2>
        ''', headers={'Content-Type': 'text/html; charset=utf-8'})

    lookup = HhDescriptions(httpx.MockTransport(answer))
    monkeypatch.setattr('vacancy_retriever.collectors.email.collector.HhDescriptions', lambda: lookup)
    html = '<table><tr><td><a href="https://hh.ru/vacancy/123?loginkey=secret">Аналитик</a><p>OSTIN, Москва</p></td></tr></table>'
    rule = SenderRule(match='noreply@hh.ru', name='hh.ru', parser='hh_ru')
    [item] = items_from_message('mail', 'mail', '1', letter('noreply@hh.ru', html), rule).items
    http = PoliteHttpClient()
    collector = EmailCollector(SourceConfig(id='mail', type='email', name='Mail'), http)
    try:
        assert collector.can_fetch_detail(item.detail_url, set(config.sources.detail_domains))
        description = collector.fetch_detail(item)
        assert description == item.text + '\n\nОбязанности:\nПоддерживать отчёты'
        assert seen == ['https://get-works.ru/job-detail.php?vac=123']
    finally:
        collector.finish_details()
        http.close()


def test_hh_copy_rejects_a_different_original_and_never_follows_redirects():
    seen = []

    def answer(request):
        seen.append(request.url.host)
        if request.url.params['vac'] == '123':
            return httpx.Response(200, text='''
                <h2 class="detail-title">Описание вакансии</h2>
                <div class="p-otstup">Чужое описание</div><a href="https://hh.ru/vacancy/456">Источник</a>
            ''')
        return httpx.Response(302, headers={'Location': 'https://hh.ru/vacancy/456'})

    for vacancy_id in ('123', '456'):
        lookup = HhDescriptions(httpx.MockTransport(answer))
        try:
            assert lookup.fetch(f'https://hh.ru/vacancy/{vacancy_id}', 'Исходное письмо') is None
        finally:
            lookup.close()
    assert seen == ['get-works.ru', 'get-works.ru']


def test_unknown_template_goes_to_mail_triage():
    """«Приглашение на интервью» from hh.ru is a status of an application (20.11.2026): the mail stage reads it."""
    load_parsers()
    rule = SenderRule(match="noreply@hh.ru", name="hh.ru", parser="hh_ru")
    result = items_from_message("yandex", "yandex", "5", letter("hh.ru <noreply@hh.ru>", "<p>Приглашение на интервью</p>",
                                                                bulk=False), rule)
    assert result.items == [] and result.triage.startswith("unknown template") and not result.unknown


def test_known_junk_goes_to_mail_triage_with_the_parsers_reason():
    load_parsers()
    rule = SenderRule(match="superjob.ru", name="SuperJob", parser="superjob")
    result = items_from_message("yandex", "yandex", "6", letter("SuperJob <nobody@host.superjob.ru>", "<p>Скидка</p>"), rule)
    assert result.items == [] and result.triage == "SuperJob promo without vacancies"


def test_vacancies_seen_in_earlier_letters_are_not_collected_again():
    load_parsers()
    rule = SenderRule(match="noreply@hh.ru", name="hh.ru", parser="hh_ru")
    html = "<table><tr><td><a href='https://hh.ru/vacancy/7'>Data Analyst</a><p>Acme</p></td></tr></table>"
    result = items_from_message("yandex", "yandex", "8", letter("hh.ru <noreply@hh.ru>", html), rule,
                                is_known=lambda external_id: external_id == "hh:7")
    [item] = result.items
    assert item.meta["ignore"].startswith("All vacancies already collected")


def test_prefilter_hides_ignored_letters(config):
    from datetime import UTC, datetime

    from vacancy_retriever.pipeline.prefilter import Prefilter
    from vacancy_retriever.storage.items import StoredRawItem

    item = StoredRawItem(1, 1, False, "yandex", "<m>", "", "promo", "promo", None, None, False,
                         {"ignore": "SuperJob promo without vacancies"}, "pending_llm", None)
    result = Prefilter(config.rules).check(item, datetime.now(UTC))
    assert (result.action, result.reason) == ("hide", "SuperJob promo without vacancies")


def test_hh_resume_recommendations_are_read_since_moscow_offices_fit():
    load_parsers()
    html = "<table><tr><td><a href='https://hh.ru/vacancy/1'>Аналитик</a><p>Монетка</p></td></tr></table>"
    [vacancy] = PARSERS["hh_ru"](letter("hh.ru <noreply@hh.ru>", html, "Подходящие вакансии для резюме: «Data Scientist»"))
    assert vacancy.url == "https://hh.ru/vacancy/1" and vacancy.extra["template"] == "hh_resume_picks"
    assert "подходящие вакансии для резюме" in vacancy.text


def test_hh_work_format_hints():
    load_parsers()
    name = "<p>Новые вакансии по автопоиску «для 🦆 vacancy-retriever ( Data Scientist )»</p>"
    row = "<table><tr><td><a href='https://hh.ru/vacancy/{n}'>Data Scientist {n}</a><p>Acme, {place}</p></td></tr></table>"
    vacancies = PARSERS["hh_ru"](letter("hh.ru <noreply@hh.ru>", name + row.format(n=9, place="Москва, м. Арбатская")
                                        + row.format(n=10, place="Казань"), "Новые вакансии"))
    assert [v.extra.get("work_format") for v in vacancies] == ["office", None], "the search name no longer means remote"
    remote = "<p>Удалённая работа</p>" + row.format(n=11, place="Москва")
    [vacancy] = PARSERS["hh_ru"](letter("hh.ru <noreply@hh.ru>", remote, "Новые вакансии"))
    assert vacancy.extra["work_format"] == "remote"


def test_first_watched_letter_raises_one_notice(tmp_paths, monkeypatch):
    from vacancy_retriever import notices
    from vacancy_retriever.collectors.email import collector as email_collector
    from vacancy_retriever.collectors.email.collector import EmailCollector
    from vacancy_retriever.config import AppConfig, MailboxConfig

    load_parsers()
    config = AppConfig(tmp_paths)
    mailbox = MailboxConfig(id="yandex", name="Yandex Mail", kind="yandex", host="h", login_env="L", password_env="P")
    html = "<table><tr><td><a href='https://hh.ru/vacancy/9'>Data Scientist</a><p>Acme</p></td></tr></table>"
    message = letter("hh.ru <noreply@hh.ru>", html, "Новые вакансии по автопоиску «для 🦆 vacancy-retriever»")
    rule = SenderRule(match="noreply@hh.ru", name="hh.ru", parser="hh_ru")
    items = items_from_message("yandex", "yandex", "77", message, rule).items
    EmailCollector._watch(config, mailbox, "76", message, items)
    assert notices.active(config.paths.notices) == [], "hh_saved_search was checked on a real letter (28.10.2026)"
    monkeypatch.setitem(email_collector.WATCHED_TEMPLATES, "hh_saved_search", "первое письмо автопоиска hh.ru")
    EmailCollector._watch(config, mailbox, "77", message, items)
    EmailCollector._watch(config, mailbox, "78", message, items)
    [notice] = notices.active(config.paths.notices)
    assert notice["id"] == "first:hh_saved_search"
    assert "«Новые вакансии по автопоиску «для 🦆 vacancy-retriever»»" in notice["text"]
    assert "<m1@example.com>" in notice["text"] and "UID 77" in notice["text"]
    assert notices.dismiss(config.paths.notices, notice["id"]) and notices.active(config.paths.notices) == []
