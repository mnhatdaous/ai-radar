"""Hugging Face: curated daily papers, and what is trending among models, datasets and Spaces."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ai_radar.config import ConfigError, SourceConfig
from ai_radar.http import FetchError
from ai_radar.models import RawItem
from ai_radar.sources.base import Source, register
from ai_radar.text import clean_html, clean_title, collapse_ws, parse_datetime, truncate

API = "https://huggingface.co/api"
MODES = ("daily_papers", "models", "datasets", "spaces")
_URL_PREFIX = {"models": "", "datasets": "datasets/", "spaces": "spaces/"}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@register
class HuggingFaceSource(Source):
    type = "huggingface"
    required = ("mode",)

    @classmethod
    def validate(cls, cfg: SourceConfig) -> None:
        super().validate(cfg)
        if cfg.opt("mode") not in MODES:
            raise ConfigError(f"source {cfg.id!r}: mode must be one of {', '.join(MODES)}")

    async def fetch(self) -> list[RawItem]:
        mode = self.opt("mode")
        limit = int(self.opt("limit", 50))
        if mode == "daily_papers":
            return await self._daily_papers(limit)
        data = await self.http.get_json(
            f"{API}/{mode}", params={"sort": "trendingScore", "limit": limit}
        )
        cutoff = self.ctx.cutoff(self.cfg)  # trending also lists old classics: keep only new ones
        items = []
        for entry in data:
            created = parse_datetime(entry.get("createdAt"))
            if created and created < cutoff:
                continue
            if item := self._repo(mode, entry, created):
                items.append(item)
        return items

    async def _daily_papers(self, limit: int) -> list[RawItem]:
        """Papers featured on the last few *completed* UTC days, best first.

        A day's list keeps growing until the day ends, so today's is skipped: yesterday's is the
        complete one, with its final upvote counts. Weekends are legitimately empty.
        """
        days = int(self.opt("days", 3))
        today = self.ctx.now.date()
        found: dict[str, RawItem] = {}
        failures = 0
        for offset in range(1, days + 1):
            day = (today - timedelta(days=offset)).isoformat()
            try:
                data = await self.http.get_json(
                    f"{API}/daily_papers", params={"date": day, "limit": limit}
                )
            except FetchError as exc:
                failures += 1
                self.warn(f"{day}: {exc}")
                continue
            for entry in data:
                if item := self._paper(entry):
                    found.setdefault(item.url, item)
        if failures == days:
            raise FetchError(f"all {days} daily-papers requests failed")
        return sorted(found.values(), key=lambda r: r.engagement, reverse=True)

    def _paper(self, entry: dict[str, Any]) -> RawItem | None:
        paper = entry.get("paper") or {}
        paper_id = paper.get("id")
        title = collapse_ws(clean_title(entry.get("title") or paper.get("title")))
        if not paper_id or not title:
            return None
        names = [a.get("name", "") for a in paper.get("authors", []) if a.get("name")]
        summary = paper.get("ai_summary") or entry.get("summary") or paper.get("summary") or ""
        upvotes = _int(paper.get("upvotes"))
        return RawItem(
            url=f"https://arxiv.org/abs/{paper_id}",
            title=title,
            summary=truncate(clean_html(summary), self.ctx.settings.summary_chars),
            author=", ".join(names[:3]) + (" et al." if len(names) > 3 else "") or None,
            # The day it was featured (what makes it news here), not its arXiv submission date.
            published_at=parse_datetime(
                paper.get("submittedOnDailyAt")
                or entry.get("publishedAt")
                or paper.get("publishedAt")
            ),
            discussion_url=f"https://huggingface.co/papers/{paper_id}",
            metrics={
                "hf_upvotes": float(upvotes),
                "hf_comments": float(_int(entry.get("numComments"))),
            },
            engagement=float(upvotes) * 3,  # ~HN points: 30 upvotes is a very good day
            tags=[str(k) for k in (paper.get("ai_keywords") or [])][:5],
        )

    def _repo(self, mode: str, entry: dict[str, Any], created: Any) -> RawItem | None:
        repo_id = entry.get("id")
        if not repo_id:
            return None
        likes = _int(entry.get("likes"))
        downloads = _int(entry.get("downloads"))
        trending = float(entry.get("trendingScore") or 0)
        pipeline = entry.get("pipeline_tag")
        tags = [t for t in (pipeline, entry.get("library_name"), entry.get("sdk")) if t]
        facts = [f"{likes:,} likes"] + ([f"{downloads:,} downloads"] if downloads else [])
        label = {"models": pipeline or "model", "datasets": "dataset", "spaces": "Space"}[mode]
        description = clean_html(entry.get("description"))
        summary = f"Trending {label} on Hugging Face · {' · '.join(facts)}."
        if description:
            summary = f"{truncate(description, 160)} {summary}"
        return RawItem(
            url=f"https://huggingface.co/{_URL_PREFIX[mode]}{repo_id}",
            title=repo_id,
            summary=summary,
            author=repo_id.split("/")[0] if "/" in repo_id else None,
            published_at=created,
            metrics={"hf_likes": float(likes), "hf_downloads": float(downloads)},
            engagement=(trending or float(likes)) / 5,
            tags=tags,
        )
