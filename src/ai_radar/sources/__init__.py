"""Importing this package registers every built-in source type."""

from ai_radar.sources import (  # noqa: F401
    arxiv,
    gdelt,
    github,
    hackernews,
    html_list,
    huggingface,
    openrouter,
    rss,
    sitemap,
)
from ai_radar.sources.base import REGISTRY, CrawlContext, Source, register

__all__ = ["REGISTRY", "CrawlContext", "Source", "register"]
