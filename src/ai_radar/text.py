"""Text and date helpers: HTML to plain text, truncation, tolerant date parsing."""

from __future__ import annotations

import email.utils
import html
import re
import time
import warnings
from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning, XMLParsedAsHTMLWarning

warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

_WS = re.compile(r"\s+")
_DROP_TAGS = ["script", "style", "noscript", "svg", "iframe", "figure", "img"]
_TAIL_NOISE = [
    # WordPress: "The post Foo appeared first on Bar."
    re.compile(r"\s*The post .{1,300}? appeared first on .{1,120}?\.?\s*$", re.I | re.S),
    # "[…] Read more", "Continue reading →"
    re.compile(
        r"\s*(?:\[\s*(?:…|\.\.\.)\s*\]|…|\.\.\.)?\s*"
        r"(?:Read more|Continue reading|Read the full (?:story|article|post))"
        r"(?:\s*(?:»|→|>>))?\s*\.?\s*$",
        re.I,
    ),
]


def collapse_ws(text: str) -> str:
    return _WS.sub(" ", text).strip()


def clean_html(value: str | None) -> str:
    """Plain text from an HTML fragment (or from text that merely contains entities)."""
    if not value:
        return ""
    text = value
    if "<" in value or "&" in value:
        soup = BeautifulSoup(value, "lxml")
        for tag in soup(_DROP_TAGS):
            tag.decompose()
        text = html.unescape(soup.get_text(" ", strip=True))
    text = collapse_ws(text)
    for pattern in _TAIL_NOISE:
        text = pattern.sub("", text)
    return text.strip()


def clean_title(value: str | None) -> str:
    return clean_html(value)


def truncate(text: str, limit: int) -> str:
    """Cut at a word boundary and add an ellipsis. Keeps stored excerpts short."""
    text = text.strip()
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space >= limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-—") + "…"


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


_FALLBACK_FORMATS = ("%Y%m%dT%H%M%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d %b %Y", "%B %d, %Y")


def parse_datetime(value: Any) -> datetime | None:
    """Best-effort parse of the many date shapes feeds and APIs produce. Always returns UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, time.struct_time):
        return datetime(*value[:6], tzinfo=UTC)
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        return _utc(datetime.fromisoformat(text))
    except ValueError:
        pass
    try:
        return _utc(email.utils.parsedate_to_datetime(text))
    except (TypeError, ValueError):
        pass
    for fmt in _FALLBACK_FORMATS:
        try:
            return _utc(datetime.strptime(text, fmt))
        except ValueError:
            continue
    return None
