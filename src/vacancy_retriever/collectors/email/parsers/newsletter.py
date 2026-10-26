"""Senders that only send newsletters / events, never vacancy lists (job fairs, platform news): every bulk letter
is a known non-vacancy template; a non-bulk (personal) letter is left untouched."""

from __future__ import annotations

from email.message import Message

from . import IgnoreLetter, ParsedVacancy, UnknownLetter, is_bulk, register_parser


@register_parser("newsletter")
def parse(message: Message) -> list[ParsedVacancy]:
    if is_bulk(message):
        raise IgnoreLetter("Newsletter / event announcement")
    raise UnknownLetter("Personal letter from a newsletter sender")
