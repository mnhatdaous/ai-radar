"""Hacker News through the Algolia search API (stories that mention AI terms and got traction)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ai_radar.http import FetchError
from ai_radar.models import RawItem
from ai_radar.sources.base import Source, register
from ai_radar.text import clean_html, clean_title, parse_datetime, truncate

API = "https://hn.algolia.com/api/v1/search_by_date"
THREAD = "https://news.ycombinator.com/item?id={}"


@register
class HackerNewsSource(Source):
    type = "hackernews"
    required = ("queries",)

    def _to_item(self, hit: dict[str, Any]) -> RawItem | None:
        object_id = str(hit.get("objectID") or "")
        title = clean_title(hit.get("title") or hit.get("story_title"))
        if not object_id or not title:
            return None
        thread = THREAD.format(object_id)
        url = hit.get("url") or thread
        points = float(hit.get("points") or 0)
        comments = float(hit.get("num_comments") or 0)
        return RawItem(
            url=url,
            title=title,
            summary=truncate(clean_html(hit.get("story_text")), self.ctx.settings.summary_chars),
            author=hit.get("author"),
            published_at=parse_datetime(hit.get("created_at_i")),
            discussion_url=thread if url != thread else None,
            metrics={"hn_points": points, "hn_comments": comments},
            engagement=points + 0.5 * comments,
        )

    async def fetch(self) -> list[RawItem]:
        window = timedelta(hours=float(self.opt("window_hours", 30)))  # overlaps daily runs
        since = int((self.ctx.now - window).timestamp())
        min_points = int(self.opt("min_points", 20))
        queries = list(self.opt("queries"))
        found: dict[str, RawItem] = {}
        failures = 0
        for query in queries:
            params = {
                "query": query,
                "tags": self.opt("hn_tag", "story"),  # story | show_hn | ask_hn
                "restrictSearchableAttributes": "title",
                "numericFilters": f"created_at_i>{since},points>={min_points}",
                "hitsPerPage": int(self.opt("hits_per_page", 40)),
            }
            try:
                data = await self.http.get_json(API, params=params)
            except FetchError as exc:
                failures += 1
                self.warn(f"query {query!r}: {exc}")
                continue
            for hit in data.get("hits", []):
                item = self._to_item(hit)
                if item is not None:
                    found.setdefault(str(hit["objectID"]), item)
        if failures == len(queries):
            raise FetchError(f"all {failures} Algolia queries failed")
        return sorted(found.values(), key=lambda r: r.engagement, reverse=True)
