"""arXiv: the newest submissions per category through the public Atom API."""

from __future__ import annotations

import feedparser

from ai_radar.http import FetchError
from ai_radar.models import RawItem
from ai_radar.sources.base import Source, register
from ai_radar.text import clean_html, clean_title, collapse_ws, parse_datetime, truncate

API = "https://export.arxiv.org/api/query"


@register
class ArxivSource(Source):
    type = "arxiv"
    required = ("categories",)

    async def fetch(self) -> list[RawItem]:
        # arXiv asks for one connection and one request every ~3 seconds.
        self.http.set_host_policy("export.arxiv.org", min_interval=3.1, max_concurrency=1)
        per_category = int(self.opt("max_results", 40))
        chars = self.ctx.settings.summary_chars
        categories = list(self.opt("categories"))
        items: list[RawItem] = []
        failures = 0
        for category in categories:
            params = {
                "search_query": f"cat:{category}",
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": per_category,
            }
            try:
                resp = await self.http.get(API, params=params)
            except FetchError as exc:
                failures += 1
                self.warn(f"{category}: {exc}")
                continue
            for entry in feedparser.parse(resp.content).entries:
                title = collapse_ws(clean_title(entry.get("title")))
                link = entry.get("link") or entry.get("id")
                if not title or not link:
                    continue
                names = [
                    a.get("name", "").strip() for a in entry.get("authors", []) if a.get("name")
                ]
                author = ", ".join(names[:3]) + (" et al." if len(names) > 3 else "") or None
                items.append(
                    RawItem(
                        url=link,
                        title=title,
                        summary=truncate(clean_html(entry.get("summary")), chars),
                        author=author,
                        published_at=parse_datetime(entry.get("published_parsed")),
                        tags=[t["term"] for t in entry.get("tags", []) if t.get("term")][:6],
                    )
                )
        if failures == len(categories):
            raise FetchError(f"all {failures} arXiv requests failed")
        return items
