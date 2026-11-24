"""HTML listing pages: collect links that match a pattern, then read each new page's metadata."""

from __future__ import annotations

import re
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup

from ai_radar.config import ConfigError, SourceConfig
from ai_radar.http import FetchError
from ai_radar.models import RawItem
from ai_radar.sources._page import fetch_pages
from ai_radar.sources.base import Source, register
from ai_radar.urls import canonicalize_url, item_id


@register
class HtmlListSource(Source):
    type = "html_list"
    required = ("url", "link_regex")

    @classmethod
    def validate(cls, cfg: SourceConfig) -> None:
        super().validate(cfg)
        try:
            re.compile(cfg.opt("link_regex"))
        except re.error as exc:
            raise ConfigError(f"source {cfg.id!r}: bad regex in link_regex: {exc}") from exc

    async def fetch(self) -> list[RawItem]:
        resp = await self.http.get(self.opt("url"), robots=True)
        soup = BeautifulSoup(resp.text, "lxml")
        pattern = re.compile(self.opt("link_regex"))
        selector = self.opt("link_selector", "a[href]")

        matched: dict[str, None] = {}
        for anchor in soup.select(selector):
            href = anchor.get("href")
            if not isinstance(href, str):
                continue
            absolute = urldefrag(urljoin(str(resp.url), href))[0]
            if pattern.search(absolute):
                matched.setdefault(absolute, None)
        if not matched:
            raise FetchError("no link on the page matches `link_regex`; did the site change?")

        targets = []
        for url in matched:  # page order: listings put the newest first
            try:
                known = item_id(canonicalize_url(url)) in self.ctx.known_ids
            except ValueError:
                continue
            if not known:
                targets.append((url, None))
        return await fetch_pages(self, targets[: int(self.opt("max_pages", 12))])
