"""One crawl run: fetch every source concurrently, normalise, filter, merge duplicates, store."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx

from ai_radar.config import AppConfig, ConfigError, SourceConfig
from ai_radar.http import HttpClient
from ai_radar.models import KINDS, Item, Mention, RawItem
from ai_radar.relevance import Taxonomy
from ai_radar.sources import REGISTRY, CrawlContext
from ai_radar.store import Store
from ai_radar.text import clean_title, truncate
from ai_radar.urls import canonicalize_url, clean_url, domain_of, item_id

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SourceResult:
    source_id: str
    name: str
    type: str
    weight: float
    order: int
    started_at: datetime
    ok: bool = False
    fetched: int = 0  # candidates the source returned
    kept: int = 0  # ...that passed the age, relevance and size filters
    new: int = 0  # ...that were not in the store yet (credited to the primary source)
    seconds: float = 0.0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)


@dataclass(slots=True)
class RunReport:
    run_id: str
    started_at: datetime
    results: list[SourceResult] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)  # merged, deduplicated items of this run
    new_items: int = 0
    finished_at: datetime | None = None

    @property
    def failed(self) -> list[SourceResult]:
        return [r for r in self.results if not r.ok]


def _describe(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timed out"
    text = str(exc).strip() or type(exc).__name__
    return text if len(text) <= 300 else text[:299] + "…"


def normalize(
    raw_items: list[RawItem], sc: SourceConfig, ctx: CrawlContext, taxonomy: Taxonomy
) -> list[Item]:
    """Clean candidates and apply the age, relevance and size limits. Order is preserved."""
    cutoff = ctx.cutoff(sc)
    out: list[Item] = []
    for raw in raw_items:
        title = clean_title(raw.title)
        if not title or not raw.url:
            continue
        try:
            canonical = canonicalize_url(raw.url)
            url = clean_url(raw.url)
        except ValueError:
            continue  # not an http(s) URL
        published = raw.published_at
        if published and published < cutoff:
            continue
        if published and published > ctx.now + timedelta(days=1):
            published = ctx.now  # scheduled posts and clock skew
        summary = truncate(raw.summary, ctx.settings.summary_chars)
        if summary.startswith(("http://", "https://")) and " " not in summary:
            summary = ""  # a bare link says nothing
        if not taxonomy.is_ai(title, summary, sc.ai_filter):
            continue
        discussion: str | None = None
        if raw.discussion_url:
            try:
                discussion = clean_url(raw.discussion_url)
            except ValueError:
                discussion = None
            if discussion == url:
                discussion = None
        out.append(
            Item(
                id=item_id(canonical),
                url=url,
                canonical_url=canonical,
                title=title,
                summary=summary,
                author=raw.author,
                published_at=published,
                first_seen_at=ctx.now,
                last_seen_at=ctx.now,
                source_id=sc.id,
                source_name=raw.publisher or sc.display_name,
                kind=raw.kind if raw.kind in KINDS else sc.kind,
                weight=sc.weight,
                topics=taxonomy.topics_for("\n".join([title, summary, " ".join(raw.tags)])),
                tags=raw.tags[:8],
                metrics=dict(raw.metrics),
                engagement=max(raw.engagement, 0.0),
                discussion_url=discussion,
                domain=domain_of(url),
            )
        )
        if len(out) >= sc.max_items:
            break
    return out


def merge_items(results: list[SourceResult]) -> list[Item]:
    """Collapse the same URL reported by several sources into one item plus mentions."""
    merged: dict[str, Item] = {}
    # Most trusted source first so it owns the item; config order breaks ties.
    for result in sorted(results, key=lambda r: (-r.weight, r.order)):
        for item in result.items:
            primary = merged.get(item.id)
            if primary is None:
                merged[item.id] = item
                continue
            keys = {*primary.metrics, *item.metrics}
            primary.metrics = {
                k: max(primary.metrics.get(k, 0.0), item.metrics.get(k, 0.0)) for k in keys
            }
            primary.engagement = max(primary.engagement, item.engagement)
            primary.topics = list(dict.fromkeys([*primary.topics, *item.topics]))[:4]
            primary.tags = list(dict.fromkeys([*primary.tags, *item.tags]))[:8]
            primary.discussion_url = primary.discussion_url or item.discussion_url
            primary.summary = primary.summary or item.summary
            dates = [d for d in (primary.published_at, item.published_at) if d]
            primary.published_at = min(dates) if dates else None
            if item.source_id != primary.source_id:
                primary.mentions.append(
                    Mention(
                        item.source_id,
                        item.source_name,
                        item.discussion_url or item.url,
                        item.title,
                        item.metrics,
                        item.first_seen_at,
                    )
                )
    return list(merged.values())


async def _run_source(
    order: int, sc: SourceConfig, ctx: CrawlContext, taxonomy: Taxonomy
) -> SourceResult:
    result = SourceResult(
        source_id=sc.id,
        name=sc.display_name,
        type=sc.type,
        weight=sc.weight,
        order=order,
        started_at=ctx.now,
    )
    source = REGISTRY[sc.type](sc, ctx)
    started = time.monotonic()
    try:
        raw = await asyncio.wait_for(source.fetch(), timeout=ctx.settings.source_timeout)
        result.fetched = len(raw)
        result.items = normalize(raw, sc, ctx, taxonomy)
        result.kept = len(result.items)
        result.ok = True
    except Exception as exc:
        result.error = _describe(exc)
        log.warning("%-28s FAILED  %s", sc.id, result.error)
        log.debug("traceback for %s", sc.id, exc_info=True)
    result.warnings = list(source.warnings)
    result.seconds = round(time.monotonic() - started, 2)
    if result.ok:
        log.info(
            "%-28s ok      fetched=%-4d kept=%-4d %.1fs",
            sc.id,
            result.fetched,
            result.kept,
            result.seconds,
        )
    return result


async def run_crawl(
    cfg: AppConfig,
    store: Store | None,
    *,
    now: datetime | None = None,
    only: list[str] | None = None,
    skip: list[str] | None = None,
    dry_run: bool = False,
    max_age_days: int | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    github_token: str | None = None,
) -> RunReport:
    """Crawl the configured sources. With ``dry_run`` (or no store) nothing is written."""
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    unknown = sorted({*(only or []), *(skip or [])} - {s.id for s in cfg.sources})
    if unknown:
        raise ConfigError(f"unknown source id(s): {', '.join(unknown)}")
    if only:
        selected = [(i, s) for i, s in enumerate(cfg.sources) if s.id in only]  # even if disabled
    else:
        selected = [(i, s) for i, s in enumerate(cfg.sources) if s.enabled]
    selected = [(i, s) for i, s in selected if s.id not in (skip or [])]

    known = store.known_ids() if store is not None else set()
    token = github_token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    report = RunReport(run_id=f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}", started_at=now)

    async with HttpClient(cfg.settings, transport=transport) as http:
        ctx = CrawlContext(
            http=http,
            settings=cfg.settings,
            now=now,
            known_ids=known,
            github_token=token,
            max_age_override=max_age_days,
        )
        report.results = list(
            await asyncio.gather(*(_run_source(i, s, ctx, cfg.taxonomy) for i, s in selected))
        )

    report.items = merge_items(report.results)
    by_source = {r.source_id: r for r in report.results}
    if dry_run or store is None:
        is_new = [item.id not in known for item in report.items]
    else:
        is_new = store.upsert_many(report.items)
    for item, fresh in zip(report.items, is_new, strict=True):
        if fresh:
            by_source[item.source_id].new += 1
    report.new_items = sum(is_new)
    report.finished_at = datetime.now(UTC).replace(microsecond=0)

    if store is not None and not dry_run:
        store.record_run(
            run_id=report.run_id,
            started_at=report.started_at,
            finished_at=report.finished_at,
            new_items=report.new_items,
            sources=[
                {
                    "source_id": r.source_id,
                    "source_name": r.name,
                    "started_at": r.started_at,
                    "ok": r.ok,
                    "fetched": r.fetched,
                    "kept": r.kept,
                    "new": r.new,
                    "seconds": r.seconds,
                    "error": r.error,
                    "warnings": r.warnings,
                }
                for r in report.results
            ],
        )
    return report
