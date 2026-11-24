"""Shared test helpers: a fake internet, a crawl context, item and feed builders."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import httpx

from ai_radar.config import AppConfig, Settings, SourceConfig
from ai_radar.http import HttpClient
from ai_radar.models import Item
from ai_radar.relevance import Taxonomy
from ai_radar.sources import REGISTRY, CrawlContext

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 10, 0, 30, tzinfo=UTC)

Handler = Callable[[httpx.Request], httpx.Response]


class Router:
    """A tiny fake internet: the first route whose needle appears in the URL answers."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, Handler]] = []
        self.requests: list[httpx.Request] = []

    def add(
        self,
        needle: str,
        *,
        text: str | None = None,
        content: bytes | None = None,
        json: Any = None,
        status: int = 200,
        headers: dict[str, str] | None = None,
        handler: Handler | None = None,
    ) -> Router:
        if handler is None:

            def handler(request: httpx.Request) -> httpx.Response:
                if json is not None:
                    return httpx.Response(status, json=json, headers=headers)
                if content is not None:
                    return httpx.Response(status, content=content, headers=headers)
                return httpx.Response(status, text=text or "", headers=headers)

        self.routes.append((needle, handler))
        return self

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for needle, handler in self.routes:
            if needle in url:
                return handler(request)
        return httpx.Response(404, text="not found")

    def hits(self, needle: str) -> list[httpx.Request]:
        return [r for r in self.requests if needle in str(r.url)]


async def no_sleep(_seconds: float) -> None:
    return None


@contextlib.asynccontextmanager
async def crawl_context(
    router: Router,
    *,
    now: datetime = NOW,
    known_ids: Any = (),
    github_token: str | None = None,
    **settings: Any,
) -> AsyncIterator[CrawlContext]:
    cfg = Settings(**{"retries": 1, "backoff_base": 0.0, **settings})
    async with HttpClient(cfg, transport=httpx.MockTransport(router), sleep=no_sleep) as http:
        yield CrawlContext(
            http=http,
            settings=cfg,
            now=now,
            known_ids=set(known_ids),
            github_token=github_token,
        )


def make_source(ctx: CrawlContext, type_: str, id_: str = "test", **options: Any):
    cfg = SourceConfig(id=id_, type=type_, **options)
    REGISTRY[type_].validate(cfg)
    return REGISTRY[type_](cfg, ctx)


def fetch(
    router: Router,
    type_: str,
    *,
    now: datetime = NOW,
    known_ids: Any = (),
    github_token: str | None = None,
    settings: dict[str, Any] | None = None,
    **options: Any,
):
    """Run one source against the fake internet. Returns (raw items, warnings)."""

    async def go():
        async with crawl_context(
            router, now=now, known_ids=known_ids, github_token=github_token, **(settings or {})
        ) as ctx:
            source = make_source(ctx, type_, **options)
            return await source.fetch(), source.warnings

    return asyncio.run(go())


_TAXONOMY: Taxonomy | None = None


def taxonomy() -> Taxonomy:
    global _TAXONOMY
    if _TAXONOMY is None:
        _TAXONOMY = Taxonomy.load(ROOT / "config" / "topics.yaml")
    return _TAXONOMY


def app_config(sources: list[dict[str, Any]], **settings: Any) -> AppConfig:
    cfgs = [SourceConfig(**s) for s in sources]
    for cfg in cfgs:
        REGISTRY[cfg.type].validate(cfg)
    return AppConfig(
        settings=Settings(**{"retries": 0, "backoff_base": 0.0, **settings}),
        sources=cfgs,
        taxonomy=taxonomy(),
    )


def make_item(**overrides: Any) -> Item:
    fields: dict[str, Any] = {
        "id": "id-1",
        "url": "https://example.com/a",
        "canonical_url": "https://example.com/a",
        "title": "A title",
        "summary": "",
        "author": None,
        "published_at": NOW - timedelta(hours=2),
        "first_seen_at": NOW,
        "last_seen_at": NOW,
        "source_id": "src",
        "source_name": "Src",
        "kind": "news",
        "weight": 0.5,
        "topics": [],
        "tags": [],
        "metrics": {},
        "engagement": 0.0,
        "discussion_url": None,
        "domain": "example.com",
        "mentions": [],
    }
    fields.update(overrides)
    return Item(**fields)


def rss(*entries: dict[str, Any], title: str = "Feed") -> str:
    """Build an RSS 2.0 document. Entry keys: title, link, date, desc, comments, categories."""
    body = ""
    for e in entries:
        body += f"<item><title>{escape(e['title'])}</title><link>{escape(e['link'])}</link>"
        if "date" in e:
            body += f"<pubDate>{e['date']}</pubDate>"
        if "desc" in e:
            body += f"<description>{escape(e['desc'])}</description>"
        if "comments" in e:
            body += f"<comments>{escape(e['comments'])}</comments>"
        for category in e.get("categories", []):
            body += f"<category>{escape(category)}</category>"
        body += "</item>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>'
        f"<title>{escape(title)}</title>{body}</channel></rss>"
    )


def html_page(
    *,
    title: str = "Page",
    description: str | None = None,
    published: str | None = None,
    extra_head: str = "",
) -> str:
    head = f"<title>{escape(title)}</title>"
    head += f'<meta property="og:title" content="{escape(title)}">'
    if description:
        head += f'<meta property="og:description" content="{escape(description)}">'
    if published:
        head += f'<meta property="article:published_time" content="{published}">'
    return f"<html><head>{head}{extra_head}</head><body><p>body</p></body></html>"


def story_item(title: str, *, source: str = "wire", **overrides: Any) -> Item:
    """An item with a unique id and URL derived from its source and title. For story tests."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    url = f"https://{source}.test/{slug}"
    fields: dict[str, Any] = {
        "id": f"{source}:{slug}",
        "url": url,
        "canonical_url": url,
        "title": title,
        "source_id": source,
        "source_name": source.title(),
        "weight": 0.6,
        "published_at": NOW - timedelta(hours=3),
    }
    fields.update(overrides)
    return make_item(**fields)


def filler_items(count: int = 60, kind: str = "news") -> list[Item]:
    """Unrelated headlines made of made-up words, so that real words in a test are rare."""
    return [
        story_item(f"zorblax{i} quendar{i} filler{i}", source=f"filler{i}", kind=kind)
        for i in range(count)
    ]
