"""ШАБЛОН точного парсера письма. Скопируйте этот файл без подчёркивания в имени, например:

    cp _template.py hh_ru.py

и допишите разбор. Пошаговая инструкция — docs/HANDBOOK.md, раздел "Точный парсер письма".

Как это работает:
1. В config/mail.yaml у отправителя пишем `parser: hh_ru` (имя из @register_parser ниже).
2. Сборщик почты вызывает функцию с письмом (email.message.Message) и получает список вакансий.
3. Каждая вакансия становится отдельным элементом для LLM-оценки (короче и дешевле, чем отдавать
   письмо целиком универсальному парсеру).
4. Если функция вернула пустой список или упала — письмо уходит в универсальный парсер (generic),
   ничего не теряется.

Образцы писем для разработки и тестов: `vacancy-retriever export-email-samples` складывает их в
samples/emails/<отправитель>/*.eml; тест tests/test_email.py сам прогоняет все образцы через парсеры.
"""

from __future__ import annotations

from email.message import Message

from ..generic import _part_text, html_to_text  # noqa: F401 - готовые помощники
from . import ParsedVacancy, register_parser


@register_parser("template_example")
def parse(message: Message) -> list[ParsedVacancy]:
    # Пример: в HTML письма каждая вакансия — блок <table class="job">, внутри ссылка с названием.
    #
    # from bs4 import BeautifulSoup
    # html = _part_text(message, "html") or ""
    # soup = BeautifulSoup(html, "lxml")
    # result = []
    # for block in soup.select("table.job"):
    #     link = block.select_one("a")
    #     result.append(ParsedVacancy(
    #         title=link.get_text(strip=True),
    #         url=link["href"],                       # ссылки linkedin.com только показываются, не открываются
    #         company=block.select_one(".company").get_text(strip=True),
    #         location=block.select_one(".location").get_text(strip=True),
    #         text=block.get_text("\n", strip=True),  # всё, что известно о вакансии, — для LLM
    #     ))
    # return result
    return []
