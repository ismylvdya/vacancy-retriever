"""LinkedIn: job alerts, "similar jobs", "X is hiring" / recommendations (jobalerts-noreply@, jobs-noreply@).

Every vacancy is a link to /jobs/view/<id>/ with the title, then a "Company · City (Format)" line and optional
flags ("Easy Apply", "Actively recruiting"). linkedin.com is never opened: no detail page, the LLM gets
exactly what the letter says. Notifications and Premium promos are known non-vacancy templates.
"""

from __future__ import annotations

import re
from email.message import Message

from . import IgnoreLetter, ParsedVacancy, UnknownLetter, clean, html_soup, register_parser, sender_address, subject

_JOB = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")
_FLAGS = ("Easy Apply", "Actively recruiting", "Be an early applicant", "Promoted")
# Known templates without vacancies (sender address or subject).
_IGNORE_SENDERS = ("notifications-noreply@linkedin.com", "@em.linkedin.com", "linkedin@e.linkedin.com")
_FORMAT = re.compile(r"^(?P<place>.*?)\s*\((?P<format>Hybrid|Remote|On-site)\)\s*$", re.I)
_FORMATS = {"hybrid": "hybrid", "remote": "remote", "on-site": "office"}
_SERBIA = re.compile(r"serbia|srbija|belgrade|beograd|novi sad|niš|nis\b|kragujevac|subotica|vojvodina", re.I)
_IGNORE_SUBJECTS = re.compile(r"appeared in \d+ search|valued member|premium|who.s viewed your profile", re.I)


def _blocks(soup) -> dict[str, tuple[str, list[str]]]:
    """job id → (title, text lines of the smallest element holding this job and no other).
    The title is the job link's text; some templates wrap the card in an empty link — then the line before
    the "Company · City" line."""
    found: dict[str, list[str]] = {}
    titles: dict[str, str] = {}
    for link in soup.find_all("a", href=True):
        match = _JOB.search(link["href"])
        if not match:
            continue
        job_id = match.group(1)
        text = clean(link.get_text(" "))
        if text and text not in _FLAGS and not text.startswith("http"):
            titles.setdefault(job_id, text)
        if job_id in found:
            continue
        block = link
        while block.parent is not None and set(_JOB.findall(str(block.parent))) <= {job_id}:
            block = block.parent
        found[job_id] = [line for line in (clean(t) for t in block.stripped_strings) if line]
    result: dict[str, tuple[str, list[str]]] = {}
    for job_id, lines in found.items():
        title = titles.get(job_id, "")
        if not title:
            place = next((i for i, line in enumerate(lines) if " · " in line), None)
            title = lines[place - 1] if place else ""
        if title:
            result[job_id] = (title, lines)
    return result


def _alert_name(soup) -> str:
    """"Your job alert for Data Analyst" / "Jobs similar to your alert" / "Recommendations based on your activity"."""
    lines = [line for line in (clean(t) for t in soup.stripped_strings) if line]
    for index, line in enumerate(lines):
        if line == "Your job alert for" and index + 1 < len(lines):
            return f"{line} {lines[index + 1]}"
        if line.startswith(("Your job alert for", "Jobs similar to your alert", "Recommendations based")):
            return line
    return ""


def location_hints(location: str) -> dict[str, str]:
    """"Belgrade (Hybrid)" → city Belgrade, country Serbia, format hybrid; "Serbia (Remote)" → country, remote."""
    hints: dict[str, str] = {}
    match = _FORMAT.match(location.strip())
    place = match.group("place") if match else location.strip()
    if match:
        hints["work_format"] = _FORMATS[match.group("format").lower()]
    if _SERBIA.search(place):
        hints["country"] = "Serbia"
        city = place.split(",")[0].strip()
        if city and city.lower() not in {"serbia", "srbija"}:
            hints["city"] = city.replace("Belgrade City", "Belgrade")
    return hints


@register_parser("linkedin")
def parse(message: Message) -> list[ParsedVacancy]:
    sender = sender_address(message)
    if any(marker in sender for marker in _IGNORE_SENDERS) or _IGNORE_SUBJECTS.search(subject(message)):
        raise IgnoreLetter("LinkedIn notification / promo")
    soup = html_soup(message)
    alert = _alert_name(soup)
    vacancies: list[ParsedVacancy] = []
    for job_id, (title, lines) in _blocks(soup).items():
        after = lines[lines.index(title) + 1:] if title in lines else lines
        place = next((line for line in after if " · " in line), "")
        if not title or not place:
            continue
        company, _, location = place.partition(" · ")
        flags = [line for line in after[:4] if line in _FLAGS]
        url = f"https://www.linkedin.com/jobs/view/{job_id}/"
        text = "\n".join(filter(None, [title, f"{company} · {location}", ", ".join(flags),
                                       f"LinkedIn — {alert}" if alert else "LinkedIn job alert"]))
        vacancies.append(ParsedVacancy(title=title, url=url, company=company.strip(), location=location.strip(),
                                       text=text, key=f"linkedin:{job_id}", extra=location_hints(location)))
    if not vacancies:
        raise UnknownLetter("LinkedIn letter without jobs")
    return vacancies
