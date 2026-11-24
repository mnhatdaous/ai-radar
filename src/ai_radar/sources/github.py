"""GitHub: new repos by topic (Search API), the trending page, and release feeds of key projects."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import feedparser
from bs4 import BeautifulSoup

from ai_radar.config import ConfigError, SourceConfig
from ai_radar.http import FetchError
from ai_radar.models import RawItem
from ai_radar.sources.base import Source, register
from ai_radar.text import clean_html, clean_title, parse_datetime, truncate

SEARCH_API = "https://api.github.com/search/repositories"
# Tags nobody wants in a digest: pre-releases, nightlies, CI builds, "bleeding edge" channels.
# The word either stands apart (v1.4-rc1, nightly-2026-11-24, trunk/abc) or, in Python-style
# versions, is glued to the number (1.4.0rc3, 2.0.0b1). A lone "a" or "b" only counts with a digit
# on each side, so build numbers such as b4567 are kept.
_PRERELEASE = re.compile(
    r"(?i)(?:^|[-._/])(?:rc|alpha|beta|nightly|canary|bleeding|dev|preview|pre|snapshot"
    r"|trunk|viable|ciflow)[-._/]?\d*(?:$|[-._/])"
    r"|(?<=\d)(?:(?:rc|alpha|beta|pre|dev)\d*|[ab]\d+)(?:$|[-._/])"
)
_FAR_PAST = datetime.min.replace(tzinfo=UTC)


@register
class GitHubSearchSource(Source):
    """Repos created recently under AI topics, ranked by stars: what is new and catching on."""

    type = "github_search"

    @classmethod
    def validate(cls, cfg: SourceConfig) -> None:
        if not cfg.opt("topics") and not cfg.opt("queries"):
            raise ConfigError(f"source {cfg.id!r} (type github_search) needs `topics` or `queries`")

    async def fetch(self) -> list[RawItem]:
        token = self.ctx.github_token
        # Search allows 10 requests/minute anonymously, 30 with a token.
        self.http.set_host_policy(
            "api.github.com", min_interval=2.2 if token else 6.5, max_concurrency=1
        )
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        since = (self.ctx.now - timedelta(days=int(self.opt("created_within_days", 7)))).strftime(
            "%Y-%m-%d"
        )
        min_stars = int(self.opt("min_stars", 25))
        queries = [f"topic:{t}" for t in self.opt("topics", [])] + list(self.opt("queries", []))
        chars = self.ctx.settings.summary_chars

        found: dict[str, RawItem] = {}
        failures = 0
        for query in queries:
            params = {
                "q": f"{query} created:>={since} stars:>={min_stars}",
                "sort": "stars",
                "order": "desc",
                "per_page": int(self.opt("per_page", 20)),
            }
            try:
                data = await self.http.get_json(SEARCH_API, params=params, headers=headers)
            except FetchError as exc:
                failures += 1
                self.warn(f"{query}: {exc}")
                continue
            for repo in data.get("items", []):
                name = repo.get("full_name")
                if not name or name in found:
                    continue
                stars = float(repo.get("stargazers_count") or 0)
                tags = list(repo.get("topics") or [])[:5]
                if repo.get("language"):
                    tags.append(repo["language"])
                found[name] = RawItem(
                    url=repo["html_url"],
                    title=name,
                    summary=truncate(clean_html(repo.get("description")), chars),
                    author=(repo.get("owner") or {}).get("login"),
                    published_at=parse_datetime(repo.get("created_at")),
                    metrics={"stars": stars, "forks": float(repo.get("forks_count") or 0)},
                    engagement=stars / 3,  # ~HN points: 300 stars in a week is a strong launch
                    tags=tags,
                )
        if failures == len(queries):
            raise FetchError(f"all {failures} GitHub searches failed")
        return sorted(found.values(), key=lambda r: r.engagement, reverse=True)


def _int(text: str | None) -> float:
    digits = re.sub(r"[^\d]", "", text or "")
    return float(digits) if digits else 0.0


def parse_trending(html_text: str, chars: int = 400) -> list[RawItem]:
    soup = BeautifulSoup(html_text, "lxml")
    items: list[RawItem] = []
    for article in soup.select("article.Box-row"):
        link = article.select_one("h2 a[href]")
        if link is None:
            continue
        slug = re.sub(r"\s+", "", str(link["href"])).strip("/")
        if slug.count("/") != 1:
            continue
        description = article.select_one("p")
        language = article.select_one("[itemprop=programmingLanguage]")
        stars = article.select_one('a[href$="/stargazers"]')
        forks = article.select_one('a[href$="/forks"]')
        today = re.search(
            r"([\d,]+)\s+stars?\s+(?:today|this week|this month)", article.get_text(" ")
        )
        metrics = {"stars": _int(stars.get_text() if stars else None)}
        if forks:
            metrics["forks"] = _int(forks.get_text())
        gained = _int(today.group(1)) if today else 0.0
        if gained:
            metrics["stars_today"] = gained
        items.append(
            RawItem(
                url=f"https://github.com/{slug}",
                title=slug,
                summary=truncate(
                    clean_html(description.get_text(" ", strip=True)) if description else "", chars
                ),
                author=slug.split("/")[0],
                metrics=metrics,
                engagement=gained / 4 if gained else metrics["stars"] / 100,
                tags=[language.get_text(strip=True)] if language else [],
            )
        )
    return items


@register
class GitHubTrendingSource(Source):
    """github.com/trending, which has no API. Honours robots.txt; one request per language."""

    type = "github_trending"

    async def fetch(self) -> list[RawItem]:
        since = self.opt("since", "daily")
        chars = self.ctx.settings.summary_chars
        languages = self.opt("languages") or [""]
        items: dict[str, RawItem] = {}
        failures = 0
        for language in languages:
            path = f"/{language}" if language else ""
            try:
                resp = await self.http.get(
                    f"https://github.com/trending{path}", params={"since": since}, robots=True
                )
            except FetchError as exc:
                failures += 1
                self.warn(f"trending{path}: {exc}")
                continue
            parsed = parse_trending(resp.text, chars)
            if not parsed:
                failures += 1
                self.warn(f"trending{path}: no repositories found; did the page layout change?")
            for item in parsed:
                items.setdefault(item.title, item)
        if failures == len(languages):
            raise FetchError(self.warnings[0] if self.warnings else "no trending data")
        return sorted(items.values(), key=lambda r: r.engagement, reverse=True)


@register
class GitHubReleasesSource(Source):
    """Release feeds of the projects you care about (inference engines, SDKs, agents...)."""

    type = "github_releases"
    required = ("repos",)

    async def _repo_releases(self, repo: str) -> list[RawItem]:
        resp = await self.http.get(f"https://github.com/{repo}/releases.atom")
        per_repo = int(self.opt("max_per_repo", 2))
        skip_prereleases = bool(self.opt("skip_prereleases", True))
        chars = self.ctx.settings.summary_chars
        short = repo.split("/")[-1]
        items: list[RawItem] = []
        for entry in feedparser.parse(resp.content).entries:
            link = (entry.get("link") or "").strip()
            name = clean_title(entry.get("title"))
            if not link or not name:
                continue
            tag = (
                unquote(link.rsplit("/releases/tag/", 1)[-1]) if "/releases/tag/" in link else name
            )
            if skip_prereleases and _PRERELEASE.search(tag):
                continue
            content = entry.get("content") or []
            body = content[0].get("value", "") if content else entry.get("summary", "")
            items.append(
                RawItem(
                    url=link,
                    title=name if short.lower() in name.lower() else f"{short} {name}",
                    summary=truncate(clean_html(body), chars),
                    published_at=parse_datetime(
                        entry.get("updated_parsed") or entry.get("published_parsed")
                    ),
                    tags=[repo],
                )
            )
            if len(items) >= per_repo:
                break
        return items

    async def fetch(self) -> list[RawItem]:
        repos = list(self.opt("repos"))
        # The host gate (config: hosts.github.com) keeps this polite: 2 at a time, spaced out.
        results = await asyncio.gather(
            *(self._repo_releases(r) for r in repos), return_exceptions=True
        )
        items: list[RawItem] = []
        failures = 0
        for repo, result in zip(repos, results, strict=True):
            if isinstance(result, BaseException):
                failures += 1
                self.warn(f"{repo}: {result}")
            else:
                items += result
        if failures == len(repos):
            raise FetchError(f"all {failures} release feeds failed")
        items.sort(key=lambda r: r.published_at or _FAR_PAST, reverse=True)
        return items
