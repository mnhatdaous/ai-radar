"""Sitemap crawler: labs without feeds still publish a sitemap.xml with URLs and dates."""

from __future__ import annotations

import re
from datetime import datetime

from lxml import etree

from ai_radar.config import ConfigError, SourceConfig
from ai_radar.http import FetchError
from ai_radar.models import RawItem
from ai_radar.sources._page import fetch_pages
from ai_radar.sources.base import Source, register
from ai_radar.text import parse_datetime
from ai_radar.urls import canonicalize_url, item_id

# Sitemaps are untrusted input: no entity expansion, no network access.
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=False)


def _child_text(element: etree._Element, name: str) -> str | None:
    for child in element:
        if isinstance(child.tag, str) and etree.QName(child).localname == name:
            return (child.text or "").strip() or None
    return None


def parse_sitemap(content: bytes) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Return ([(url, lastmod)], [child sitemap urls]) for a urlset or a sitemap index."""
    root = etree.fromstring(content, parser=_PARSER)
    if root is None:
        raise ValueError("empty document")
    urls: list[tuple[str, str | None]] = []
    children: list[str] = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = etree.QName(element).localname
        if name not in {"url", "sitemap"}:
            continue
        loc = _child_text(element, "loc")
        if not loc:
            continue
        if name == "url":
            urls.append((loc, _child_text(element, "lastmod")))
        else:
            children.append(loc)
    return urls, children


def date_from_url(url: str, pattern: re.Pattern[str] | None, fmt: str) -> datetime | None:
    """Some sites encode the date in the slug (…/news261017 for 2026-10-17)."""
    if pattern is None:
        return None
    match = pattern.search(url)
    if not match:
        return None
    try:
        return parse_datetime(datetime.strptime(match.group(1), fmt))
    except ValueError:
        return None


@register
class SitemapSource(Source):
    type = "sitemap"
    required = ("url", "include")

    @classmethod
    def validate(cls, cfg: SourceConfig) -> None:
        super().validate(cfg)
        for key in ("include", "exclude", "date_regex", "child_include"):
            if cfg.opt(key):
                try:
                    re.compile(cfg.opt(key))
                except re.error as exc:
                    raise ConfigError(f"source {cfg.id!r}: bad regex in {key}: {exc}") from exc

    async def _load(self, url: str, depth: int = 0) -> list[tuple[str, str | None]]:
        resp = await self.http.get(url)
        try:
            urls, children = parse_sitemap(resp.content)
        except (etree.XMLSyntaxError, ValueError) as exc:
            raise FetchError(f"invalid sitemap XML: {exc}") from exc
        if children and depth < 2:
            child_filter = self.opt("child_include")
            wanted = [c for c in children if not child_filter or re.search(child_filter, c)]
            for child in wanted[: int(self.opt("max_children", 6))]:
                try:
                    urls += await self._load(child, depth + 1)
                except FetchError as exc:
                    self.warn(f"child sitemap {child}: {exc}")
        return urls

    async def fetch(self) -> list[RawItem]:
        include = re.compile(self.opt("include"))
        exclude = re.compile(self.opt("exclude")) if self.opt("exclude") else None
        date_re = re.compile(self.opt("date_regex")) if self.opt("date_regex") else None
        date_fmt = self.opt("date_format", "%y%m%d")
        cutoff = self.ctx.cutoff(self.cfg)

        matched = [
            (loc, lastmod)
            for loc, lastmod in await self._load(self.opt("url"))
            if include.search(loc) and not (exclude and exclude.search(loc))
        ]
        if not matched:
            raise FetchError("no sitemap URL matches `include`; did the site change?")

        candidates: list[tuple[str, datetime | None]] = []
        for loc, lastmod in matched:
            when = parse_datetime(lastmod) or date_from_url(loc, date_re, date_fmt)
            if when and when < cutoff:
                continue
            try:
                known = item_id(canonicalize_url(loc)) in self.ctx.known_ids
            except ValueError:
                continue
            if not known:  # never re-fetch a page we already have
                candidates.append((loc, when))
        candidates.sort(key=lambda c: c[1] or self.ctx.now, reverse=True)
        return await fetch_pages(self, candidates[: int(self.opt("max_pages", 12))])
