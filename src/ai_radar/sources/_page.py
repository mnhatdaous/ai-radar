"""Page-level metadata for sources that only know URLs (sitemaps, HTML listings)."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from ai_radar.http import FetchError
from ai_radar.models import RawItem
from ai_radar.text import clean_html, clean_title, parse_datetime, truncate

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ai_radar.sources.base import Source

MAX_HTML_CHARS = 600_000  # <head> metadata is at the top; no need to parse megabytes of app shell
_SEPARATORS = (" | ", " \\ ", " — ", " – ", " - ", " · ", " :: ", " / ")


@dataclass(slots=True)
class PageMeta:
    title: str | None
    description: str | None
    published_at: datetime | None
    author: str | None


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        for attr in ("property", "name", "itemprop"):
            tag = soup.find("meta", attrs={attr: name})
            content = tag.get("content") if tag else None
            if isinstance(content, str) and content.strip():
                return content.strip()
    return None


def _find_key(node: Any, keys: tuple[str, ...]) -> str | None:
    if isinstance(node, dict):
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value:
                return value
        node = list(node.values())
    if isinstance(node, list):
        for child in node:
            found = _find_key(child, keys)
            if found:
                return found
    return None


def parse_page_meta(html_text: str) -> PageMeta:
    """Title, description, publish date and author from OpenGraph / JSON-LD / <time> tags."""
    soup = BeautifulSoup(html_text[:MAX_HTML_CHARS], "lxml")
    title = _meta(soup, "og:title", "twitter:title") or (
        soup.title.get_text() if soup.title else None
    )
    description = _meta(soup, "og:description", "twitter:description", "description")
    published = _meta(
        soup,
        "article:published_time",
        "og:article:published_time",
        "datePublished",
        "pubdate",
        "date",
        "DC.date.issued",
    )
    if not published:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except ValueError:
                continue
            published = _find_key(data, ("datePublished", "dateCreated"))
            if published:
                break
    if not published:
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag is not None:
            published = str(time_tag["datetime"])
    author = _meta(soup, "author", "article:author")
    if author and author.startswith("http"):
        author = None  # profile URL, not a name
    return PageMeta(
        title=clean_title(title) or None,
        description=clean_html(description) or None,
        published_at=parse_datetime(published),
        author=author,
    )


def slug_title(url: str) -> str:
    slug = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    return re.sub(r"[-_]+", " ", slug).strip().title() or url


def strip_site_suffix(title: str, brands: Iterable[str | None]) -> str:
    """'Big news | Acme' -> 'Big news' when 'Acme' looks like the site's own name."""

    def norm(value: str) -> str:
        return re.sub(r"[^a-z0-9]", "", value.lower())

    names = [n for n in (norm(b) for b in brands if b) if len(n) >= 3]
    for sep in _SEPARATORS:
        if sep in title:
            head, _, tail = title.rpartition(sep)
            tail_norm = norm(tail)
            if (
                head.strip()
                and len(tail_norm) >= 3
                and any(tail_norm in n or n in tail_norm for n in names)
            ):
                return head.strip()
    return title


async def fetch_pages(source: Source, targets: list[tuple[str, datetime | None]]) -> list[RawItem]:
    """Fetch pages (robots.txt permitting) and turn their metadata into items.

    ``targets`` are (url, date hint) pairs. One bad page only produces a warning; the source
    fails only when every page failed.
    """
    if not targets:
        return []
    cutoff = source.ctx.cutoff(source.cfg)
    chars = source.ctx.settings.summary_chars
    host_label = (urlsplit(targets[0][0]).hostname or "").removeprefix("www.").split(".")[0]
    brands = [source.cfg.display_name, source.cfg.opt("brand"), host_label]
    require_date = bool(source.cfg.opt("require_date", False))  # skip hubs and landing pages

    async def one(url: str, hint: datetime | None) -> RawItem | None:
        resp = await source.http.get(url, robots=True)
        meta = parse_page_meta(resp.text)
        if require_date and meta.published_at is None:
            return None  # category hubs and landing pages declare no publish date
        published = meta.published_at or hint
        if published and published < cutoff:
            return None  # an old page that was merely edited recently
        title = strip_site_suffix(meta.title or slug_title(url), brands)
        return RawItem(
            url=url,
            title=title,
            summary=truncate(meta.description or "", chars),
            author=meta.author,
            published_at=published,
        )

    results = await asyncio.gather(*(one(u, h) for u, h in targets), return_exceptions=True)
    items: list[RawItem] = []
    failures: list[str] = []
    for (url, _), result in zip(targets, results, strict=True):
        if isinstance(result, BaseException):
            failures.append(f"{url}: {result}")
        elif result is not None:
            items.append(result)
    for failure in failures:
        source.warn(failure)
    if failures and len(failures) == len(targets):
        raise FetchError(f"all {len(targets)} page fetches failed; first: {failures[0]}")
    return items
