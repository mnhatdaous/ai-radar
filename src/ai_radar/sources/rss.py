"""RSS / Atom feeds, plus small flavors for sites whose feeds need special handling."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import feedparser
from bs4 import BeautifulSoup

from ai_radar.config import ConfigError, SourceConfig
from ai_radar.http import FetchError
from ai_radar.models import RawItem
from ai_radar.sources.base import Source, register
from ai_radar.text import clean_html, clean_title, parse_datetime, truncate

FLAVORS = ("plain", "lobsters", "reddit", "producthunt")
_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)
_REDDIT_SUBMITTED = re.compile(
    r"\s*submitted by\s+\S+(?:\s+to\s+\S+)?\s*\[link\]\s*\[comments\]\s*$", re.I
)


def _tags(entry: Any) -> list[str]:
    terms = (clean_title(t.get("term")) for t in entry.get("tags", []) if t.get("term"))
    return [t for t in terms if t][:8]


def _anchor_href(soup: BeautifulSoup, label: str) -> str | None:
    tag = soup.find("a", string=label)
    href = tag.get("href") if tag else None
    return href if isinstance(href, str) else None


def entry_to_item(entry: Any, flavor: str, rank: int, summary_chars: int) -> RawItem | None:
    link = (entry.get("link") or "").strip()
    title = clean_title(entry.get("title"))
    if not link or not title:
        return None
    content = entry.get("content") or []
    body_html = content[0].get("value", "") if content else ""
    summary_html = entry.get("summary") or body_html
    published = parse_datetime(
        entry.get("published_parsed") or entry.get("updated_parsed") or entry.get("published")
    )
    author = (entry.get("author") or "").strip().lstrip("/")[:80] or None
    item = RawItem(
        url=link,
        title=title,
        summary=truncate(clean_html(summary_html), summary_chars),
        author=author,
        published_at=published,
        tags=_tags(entry),
    )

    if flavor == "lobsters":
        # <link> is the article, <comments> the Lobsters thread.
        comments = (entry.get("comments") or "").strip()
        if comments and comments != link:
            item.discussion_url = comments
    elif flavor == "reddit":
        # <link> is the thread; the post body carries [link] (target) and [comments].
        soup = BeautifulSoup(body_html, "lxml")
        target = _anchor_href(soup, "[link]")
        thread = _anchor_href(soup, "[comments]") or link
        item.url = target or thread
        item.discussion_url = thread if item.url != thread else None
        item.summary = truncate(_REDDIT_SUBMITTED.sub("", clean_html(body_html)), summary_chars)
        item.metrics = {"reddit_rank": float(rank)}
        item.engagement = 200.0 / rank  # the feed is "top of the day": position is the signal
        # A multireddit (r/a+b+c) feed labels every post with its subreddit.
        label = next((t.get("label") for t in entry.get("tags", []) if t.get("label")), None)
        if label:
            item.publisher = clean_title(label)
    elif flavor == "producthunt":
        soup = BeautifulSoup(body_html or summary_html, "lxml")
        first = soup.find("p")
        tagline = clean_html(first.get_text(" ", strip=True)) if first else ""
        if tagline:
            item.title = f"{title} — {tagline}"
            item.summary = truncate(tagline, summary_chars)
    return item


def parse_feed(
    content: bytes | str, *, flavor: str = "plain", summary_chars: int = 400
) -> list[RawItem]:
    parsed = feedparser.parse(content)
    if not parsed.entries and (parsed.bozo or not parsed.version):
        # Not a feed at all (a login page, an error page, ...). A real but empty feed has a version.
        reason = parsed.bozo_exception if parsed.bozo else "unrecognised document"
        raise ValueError(f"not a valid feed ({reason})")
    items = []
    for rank, entry in enumerate(parsed.entries, start=1):
        item = entry_to_item(entry, flavor, rank, summary_chars)
        if item is not None:
            items.append(item)
    return items


@register
class RssSource(Source):
    type = "rss"
    required = ("url",)

    @classmethod
    def validate(cls, cfg: SourceConfig) -> None:
        super().validate(cfg)
        flavor = cfg.opt("flavor", "plain")
        if flavor not in FLAVORS:
            raise ConfigError(f"source {cfg.id!r}: flavor must be one of {', '.join(FLAVORS)}")

    async def fetch(self) -> list[RawItem]:
        flavor = self.opt("flavor", "plain")
        resp = await self.http.get(self.opt("url"))
        try:
            items = parse_feed(
                resp.content, flavor=flavor, summary_chars=self.ctx.settings.summary_chars
            )
        except ValueError as exc:
            raise FetchError(str(exc)) from exc
        if flavor != "reddit":  # reddit order is the signal; everything else: newest first
            items.sort(key=lambda r: r.published_at or _FAR_FUTURE, reverse=True)
        return items
