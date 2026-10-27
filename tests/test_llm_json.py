"""Model replies that are almost JSON."""

import pytest

from vacancy_retriever.llm.base import LlmError, parse_json_object


def test_unescaped_quotes_inside_strings_are_repaired():
    reply = '{"results": [{"input_id": 1, "summary_ru": "Компания "Пульс" ищет аналитика", "x": "a "b" c"}]}'
    data = parse_json_object(reply)
    assert data["results"][0]["summary_ru"] == 'Компания "Пульс" ищет аналитика'
    assert data["results"][0]["x"] == 'a "b" c'


def test_fences_and_prose_are_tolerated():
    assert parse_json_object('Here:\n```json\n{"a": 1}\n```') == {"a": 1}


def test_truly_broken_json_is_still_an_error():
    with pytest.raises(LlmError) as info:
        parse_json_object('{"results": [{"input_id": 1, ')
    assert info.value.kind == "parse"
