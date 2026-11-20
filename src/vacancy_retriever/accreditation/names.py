"""Company names and INNs: normalising legal names, transliteration of brands, the INN check digit."""

from __future__ import annotations

import re

# Longest first: «публичное акционерное общество» before «акционерное общество».
_LEGAL_FORMS = sorted([
    "общество с ограниченной ответственностью", "публичное акционерное общество", "непубличное акционерное общество",
    "закрытое акционерное общество", "открытое акционерное общество", "акционерное общество",
    "автономная некоммерческая организация", "некоммерческая организация", "индивидуальный предприниматель",
    "федеральное государственное унитарное предприятие", "государственное унитарное предприятие",
    "федеральное государственное бюджетное учреждение", "государственная корпорация", "фонд",
    "ооо", "ао", "пао", "зао", "оао", "нао", "ано", "ип", "фгуп", "гуп", "фгбу",
    "llc", "ltd", "limited", "inc", "jsc", "pjsc", "gmbh", "d o o", "doo", "corp", "co",
], key=len, reverse=True)
_FORMS = re.compile(r"(?<![\w])(" + "|".join(re.escape(f) for f in _LEGAL_FORMS) + r")(?![\w])")
_NON_WORD = re.compile(r"[^\w]+")
_ONLY_DIGITS = re.compile(r"\D")


def normalize(name: str) -> str:
    """«ООО "СберТех"» → «сбертех»: lower case, ё → е, no quotes, legal forms or punctuation."""
    text = (name or "").lower().replace("ё", "е")
    text = _NON_WORD.sub(" ", text).replace("_", " ")
    text = _FORMS.sub(" ", text)
    return " ".join(text.split())


def compact(name: str) -> str:
    """Without spaces: «сбер тех» and «сбертех» are the same name."""
    return normalize(name).replace(" ", "")


_TRANSLIT = [
    ("shch", "щ"), ("sch", "щ"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ya", "я"),
    ("yu", "ю"), ("yo", "е"), ("ye", "е"), ("ia", "ия"), ("x", "кс"), ("w", "в"), ("q", "к"), ("j", "дж"),
    ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"), ("i", "и"),
    ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"),
    ("u", "у"), ("v", "в"), ("y", "й"), ("z", "з"),
]


def to_cyrillic(text: str) -> str:
    """A rough Latin → Cyrillic guess («Yandex» → «яндекс», «Ozon» → «озон») — only to find candidates; the LLM decides."""
    result, rest = [], text.lower()
    while rest:
        for latin, cyrillic in _TRANSLIT:
            if rest.startswith(latin):
                result.append(cyrillic)
                rest = rest[len(latin):]
                break
        else:
            result.append(rest[0])
            rest = rest[1:]
    return "".join(result).replace("йа", "я").replace("ийй", "ий")


def inn_digits(value: str | None) -> str:
    return _ONLY_DIGITS.sub("", value or "")


def valid_inn(value: str | None) -> bool:
    """The check digits of a 10-digit (organisation) or 12-digit INN: a made-up number almost never passes."""
    inn = inn_digits(value)
    if len(inn) == 10:
        weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
        return int(inn[9]) == sum(w * int(d) for w, d in zip(weights, inn)) % 11 % 10
    if len(inn) == 12:
        w11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        w12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        c11 = sum(w * int(d) for w, d in zip(w11, inn)) % 11 % 10
        c12 = sum(w * int(d) for w, d in zip(w12, inn)) % 11 % 10
        return int(inn[10]) == c11 and int(inn[11]) == c12
    return False


_SHORT = [(re.compile(rf"(?i)\b{full}\b"), short) for full, short in (
    ("общество с ограниченной ответственностью", "ООО"), ("публичное акционерное общество", "ПАО"),
    ("непубличное акционерное общество", "НАО"), ("закрытое акционерное общество", "ЗАО"),
    ("открытое акционерное общество", "ОАО"), ("акционерное общество", "АО"),
    ("автономная некоммерческая организация", "АНО"))]


def short_name(name: str) -> str:
    """«ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЯНДЕКС"» → «ООО "ЯНДЕКС"» (fewer tokens for the LLM, easier to read)."""
    for pattern, short in _SHORT:
        name = pattern.sub(short, name)
    return " ".join(name.split())
