from __future__ import annotations

import re
from datetime import datetime

from bs4 import BeautifulSoup, Tag

from .base import CollectContext, Collector, CollectResult, RawItem, SourceCursor, SourceError
from .registry import register

TELEGRAM_BASE = "https://telegram.me"

RVC_CHANNELS = {"revacancy", "revacancy_global"}
_FLAG = re.compile(r"[\U0001F1E6-\U0001F1FF]{2}")
_FLAG_TO_LANG = {
    "🇬🇧": "en", "🇺🇸": "en", "🇷🇺": "ru", "🇷🇸": "sr", "🇩🇪": "de", "🇫🇷": "fr", "🇪🇸": "es",
    "🇮🇹": "it", "🇵🇱": "pl", "🇺🇦": "uk", "🇰🇿": "kk", "🇳🇱": "nl", "🇵🇹": "pt", "🇹🇷": "tr",
    "🇨🇳": "zh", "🇯🇵": "ja", "🇨🇿": "cs", "🇬🇪": "ka", "🇦🇲": "hy", "🇮🇱": "he", "🇸🇦": "ar",
}
_RVC_LANG_LINE = re.compile(r"(Требуемые языки|Required languages):\s*([^\n]*)")
_RVC_GRADES = ("intern", "junior", "middle", "senior", "lead", "head", "director", "c-level")


def post_url(channel: str, post_id: str) -> str:
    return f"{TELEGRAM_BASE}/{channel}/{post_id}"


def message_text(node: Tag | None) -> str:
    if node is None:
        return ""
    for br in node.find_all("br"):
        br.replace_with("\n")
    return node.get_text().strip()


def parse_channel_page(html: str, channel: str) -> tuple[list[RawItem], str | None]:
    """Posts on one telegram.me/s/<channel> page (oldest first) and the channel avatar URL."""
    soup = BeautifulSoup(html, "lxml")
    avatar = soup.select_one(".tgme_page_photo_image img, .tgme_channel_info_header img")
    items: list[RawItem] = []
    for message in soup.select(".tgme_widget_message[data-post]"):
        _, _, post_id = message["data-post"].partition("/")
        if not post_id.isdigit():
            continue
        text = message_text(message.select_one(".tgme_widget_message_text"))
        if not text:
            continue
        time_tag = message.select_one(".tgme_widget_message_date time[datetime]") or message.select_one(
            "time[datetime]"
        )
        published = datetime.fromisoformat(time_tag["datetime"]) if time_tag else None
        item = RawItem(
            source_id="",
            external_id=post_id,
            url=post_url(channel, post_id),
            text=text,
            published_at=published,
            multi=True,
        )
        if channel.lower() in RVC_CHANNELS:
            enrich_rvc(item)
        items.append(item)
    return items, (avatar.get("src") if avatar else None)


def enrich_rvc(item: RawItem) -> None:
    """RVC posts are one vacancy each with a structured first line."""
    first_line = item.text.split("\n", 1)[0].strip()
    item.multi = False
    item.meta["rvc"] = True
    item.meta["grade_emoji"] = first_line[:1] if first_line else ""
    title = first_line.lstrip("🟩🟨🟥🟪⬜️️ ").split("|")[0].strip()
    item.title = title
    tokens = [part.strip().lower() for part in first_line.split("|")]
    item.meta["grade"] = next((t for t in tokens if t in _RVC_GRADES), "")
    lang_match = _RVC_LANG_LINE.search(item.text)
    if lang_match:
        flags = _FLAG.findall(lang_match.group(2))
        item.meta["required_languages"] = sorted({_FLAG_TO_LANG.get(f, f) for f in flags})


@register
class TelegramCollector(Collector):
    type_name = "telegram"

    @property
    def channel(self) -> str:
        return self.source.params.get("channel") or self.source.id

    def page_url(self, before: str | None) -> str:
        base = f"{TELEGRAM_BASE}/s/{self.channel}"
        return f"{base}?before={before}" if before else base

    def collect(self, ctx: CollectContext) -> CollectResult:
        last_id = int(ctx.cursor.last_id) if ctx.cursor.last_id else None
        collected: dict[int, RawItem] = {}
        before: str | None = None
        icon: str | None = None
        reached_end = False
        for page in range(ctx.max_pages):
            ctx.check_cancel()
            ctx.on_page(page + 1)
            html = self.http.get_text(self.page_url(before))
            posts, avatar = parse_channel_page(html, self.channel)
            icon = icon or avatar
            if page == 0 and not posts and "tgme_channel_info" not in html:
                raise SourceError(f"telegram.me/s/{self.channel}: no channel preview (private or renamed?)")
            if not posts:
                reached_end = True
                break
            for post in posts:
                post_id = int(post.external_id)
                too_old = post.published_at is not None and post.published_at < ctx.since
                if (last_id is not None and post_id <= last_id) or too_old:
                    reached_end = True
                    continue
                post.source_id = self.source.id
                collected[post_id] = post
            if reached_end or (ctx.limit and len(collected) >= ctx.limit):
                reached_end = True
                break
            before = str(min(int(p.external_id) for p in posts))
        items = sorted(collected.values(), key=lambda p: int(p.external_id), reverse=True)
        if ctx.limit:
            items = items[: ctx.limit]
        newest = str(max(int(i.external_id) for i in items)) if items else ctx.cursor.last_id
        return CollectResult(
            items=items,
            status="ok" if reached_end else "truncated",
            new_cursor=SourceCursor(last_id=newest),
            icon_url=icon,
        )
