"""GDELT DOC API: broad coverage of outlets that offer no RSS (title and URL only, so filter hard)."""

from __future__ import annotations

from ai_radar.http import FetchError
from ai_radar.models import RawItem
from ai_radar.sources.base import Source, register
from ai_radar.text import clean_title, parse_datetime

API = "https://api.gdeltproject.org/api/v2/doc/doc"


@register
class GdeltSource(Source):
    type = "gdelt"
    required = ("queries",)

    async def fetch(self) -> list[RawItem]:
        # GDELT allows one request every 5 seconds.
        self.http.set_host_policy("api.gdeltproject.org", min_interval=6.0, max_concurrency=1)
        excluded = {d.lower().removeprefix("www.") for d in self.opt("exclude_domains", [])}
        queries = list(self.opt("queries"))
        found: dict[str, RawItem] = {}
        failures = 0
        for query in queries:
            params = {
                "query": f"({query}) sourcelang:english",
                "mode": "artlist",
                "format": "json",
                "maxrecords": int(self.opt("max_records", 100)),
                "timespan": self.opt("timespan", "1d"),
                "sort": "datedesc",
            }
            try:
                data = await self.http.get_json(API, params=params)
            except FetchError as exc:
                failures += 1
                self.warn(f"{query!r}: {exc}")
                continue
            for article in data.get("articles", []):
                url = article.get("url")
                title = clean_title(article.get("title"))
                domain = (article.get("domain") or "").lower().removeprefix("www.")
                if not url or not title or domain in excluded:
                    continue
                found.setdefault(
                    url,
                    RawItem(
                        url=url,
                        title=title,
                        published_at=parse_datetime(article.get("seendate")),
                        publisher=domain or None,
                    ),
                )
        if failures == len(queries):
            raise FetchError(self.warnings[0] if self.warnings else "GDELT request failed")
        return list(found.values())
