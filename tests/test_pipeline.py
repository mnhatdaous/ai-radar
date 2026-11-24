import asyncio
from datetime import timedelta

import httpx
import pytest

from ai_radar.config import ConfigError, Settings, SourceConfig
from ai_radar.models import RawItem
from ai_radar.pipeline import SourceResult, merge_items, normalize, run_crawl
from ai_radar.sources import REGISTRY, CrawlContext, Source
from ai_radar.store import Store
from ai_radar.urls import canonicalize_url, item_id
from helpers import NOW, Router, app_config, make_item, rss, taxonomy

DAY = timedelta(days=1)
CTX = CrawlContext(http=None, settings=Settings(), now=NOW)  # type: ignore[arg-type]


def sc(**overrides) -> SourceConfig:
    return SourceConfig(**{"id": "s", "type": "rss", "name": "Source", **overrides})


def raw(**overrides) -> RawItem:
    fields = {
        "url": "https://e.test/a",
        "title": "OpenAI ships GPT-5",
        "published_at": NOW - timedelta(hours=3),
    }
    return RawItem(**{**fields, **overrides})


def norm(raws: list[RawItem], **cfg):
    return normalize(raws, sc(**cfg), CTX, taxonomy())


# ---- normalize ---------------------------------------------------------------------------


def test_the_age_window_applies_per_source() -> None:
    raws = [
        raw(url="https://e.test/new", published_at=NOW - DAY),
        raw(url="https://e.test/old", published_at=NOW - 5 * DAY),
        raw(url="https://e.test/undated", published_at=None),
    ]
    assert [i.url for i in norm(raws)] == ["https://e.test/new", "https://e.test/undated"]
    assert len(norm(raws, max_age_days=30)) == 3
    assert (
        len(
            normalize(
                raws,
                sc(),
                CrawlContext(http=None, settings=Settings(), now=NOW, max_age_override=30),
                taxonomy(),
            )
        )
        == 3
    )  # type: ignore[arg-type]


def test_the_relevance_filter_drops_unrelated_items() -> None:
    raws = [raw(), raw(url="https://e.test/b", title="Local bakery wins award")]
    assert [i.title for i in norm(raws, ai_filter="loose")] == ["OpenAI ships GPT-5"]
    assert len(norm(raws)) == 2


def test_max_items_keeps_the_first_ones() -> None:
    raws = [raw(url=f"https://e.test/{n}", title=f"OpenAI item {n}") for n in range(5)]
    assert [i.title for i in norm(raws, max_items=2)] == ["OpenAI item 0", "OpenAI item 1"]


def test_urls_are_validated_and_cleaned() -> None:
    raws = [raw(url="javascript:alert(1)"), raw(url="https://e.test/a?utm_source=x&id=1#frag")]
    [item] = norm(raws)
    assert item.url == "https://e.test/a?id=1"
    assert item.canonical_url == "https://e.test/a?id=1"
    assert item.id == item_id(canonicalize_url("https://e.test/a?id=1"))


def test_odd_dates_summaries_and_links_are_tamed() -> None:
    [future] = norm([raw(published_at=NOW + 10 * DAY)])
    assert future.published_at == NOW  # scheduled posts and clock skew

    [bare] = norm([raw(summary="https://redlib.example/r/x/comments/1")])
    assert bare.summary == ""  # a bare link says nothing

    [same] = norm([raw(discussion_url="https://e.test/a")])
    assert same.discussion_url is None  # the thread is the item


def test_kind_publisher_and_topics() -> None:
    raws = [
        raw(kind="paper", publisher="r/LocalLLaMA", title="Anthropic raises $2 billion in funding")
    ]
    [item] = norm(raws, kind="discussion")
    assert (item.kind, item.source_name, item.source_id) == ("paper", "r/LocalLLaMA", "s")
    assert "business" in item.topics
    [fallback] = norm([raw(kind="weird")], kind="discussion")
    assert fallback.kind == "discussion"  # unknown kinds fall back to the source's
    assert norm([raw()])[0].source_name == "Source"


# ---- merge -------------------------------------------------------------------------------


def result(source_id: str, weight: float, order: int, *items) -> SourceResult:
    return SourceResult(
        source_id=source_id,
        name=source_id.title(),
        type="rss",
        weight=weight,
        order=order,
        started_at=NOW,
        ok=True,
        items=list(items),
    )


def test_merge_gives_the_item_to_the_most_trusted_source() -> None:
    thread = "https://news.ycombinator.com/item?id=1"
    hn = make_item(
        source_id="hn",
        source_name="Hacker News",
        weight=0.5,
        kind="discussion",
        metrics={"hn_points": 80},
        engagement=80,
        discussion_url=thread,
        published_at=NOW - timedelta(hours=3),
        topics=["llm"],
    )
    lab = make_item(
        source_id="lab",
        source_name="Lab",
        weight=1.0,
        kind="blog",
        summary="Official",
        published_at=NOW - timedelta(hours=5),
        topics=["agents"],
    )
    [item] = merge_items([result("hn", 0.5, 0, hn), result("lab", 1.0, 1, lab)])
    assert (item.source_id, item.kind, item.summary) == ("lab", "blog", "Official")
    assert item.metrics == {"hn_points": 80}
    assert item.engagement == 80
    assert item.discussion_url == thread
    assert item.published_at == NOW - timedelta(hours=5)
    assert item.topics == ["agents", "llm"]
    assert [(m.source_id, m.url) for m in item.mentions] == [("hn", thread)]


def test_merge_does_not_mention_a_source_twice() -> None:
    first = make_item(source_id="arxiv", engagement=1)
    again = make_item(source_id="arxiv", engagement=5)  # cross-listed in a second category
    [item] = merge_items([result("arxiv", 0.3, 0, first, again)])
    assert item.mentions == []
    assert item.engagement == 5


# ---- whole runs --------------------------------------------------------------------------

LAB_FEED = rss(
    {
        "title": "Lab ships a new LLM",
        "link": "https://shared.test/story",
        "date": "Mon, 09 Nov 2026 20:00:00 GMT",
        "desc": "Official post",
    },
    {
        "title": "Lab own post",
        "link": "https://lab.test/own",
        "date": "Mon, 09 Nov 2026 21:00:00 GMT",
    },
)
NEWS_FEED = rss(
    {
        "title": "Coverage: lab ships LLM",
        "link": "https://www.shared.test/story/?utm_medium=rss",
        "date": "Mon, 09 Nov 2026 22:00:00 GMT",
    },
    {
        "title": "Ancient AI history",
        "link": "https://news.test/ancient",
        "date": "Fri, 02 Oct 2026 10:00:00 GMT",
    },
    {
        "title": "Local bakery wins award",
        "link": "https://news.test/bakery",
        "date": "Mon, 09 Nov 2026 22:00:00 GMT",
    },
)


def crawl_config():
    return app_config(
        [
            {
                "id": "lab",
                "name": "Lab",
                "type": "rss",
                "url": "https://lab.test/feed.xml",
                "kind": "blog",
                "weight": 1.0,
            },
            {
                "id": "news",
                "name": "News",
                "type": "rss",
                "url": "https://news.test/feed.xml",
                "weight": 0.6,
                "ai_filter": "loose",
            },
            {"id": "broken", "type": "rss", "url": "https://broken.test/feed.xml"},
            {"id": "off", "type": "rss", "url": "https://off.test/feed.xml", "enabled": False},
        ]
    )


def crawl(cfg, store, router, **kw):
    now = kw.pop("now", NOW)
    return asyncio.run(run_crawl(cfg, store, now=now, transport=httpx.MockTransport(router), **kw))


def make_router() -> Router:
    return (
        Router()
        .add("lab.test/feed", text=LAB_FEED)
        .add("news.test/feed", text=NEWS_FEED)
        .add("broken.test", status=500)
    )


def test_a_run_isolates_failures_and_dedupes_across_sources() -> None:
    router, store = make_router(), Store()
    report = crawl(crawl_config(), store, router)

    lab, news, broken = report.results
    assert [r.source_id for r in report.results] == [
        "lab",
        "news",
        "broken",
    ]  # the disabled one never ran
    assert router.hits("off.test") == []
    assert (lab.ok, lab.fetched, lab.kept) == (True, 2, 2)
    assert (news.ok, news.fetched, news.kept) == (True, 3, 1)  # ancient + off-topic items dropped
    assert broken.ok is False and "HTTP 500" in (broken.error or "")
    assert [r.source_id for r in report.failed] == ["broken"]

    assert report.new_items == 2
    assert (lab.new, news.new) == (2, 0)  # the shared story belongs to the more trusted source
    shared = next(i for i in report.items if i.url == "https://shared.test/story")
    assert (shared.source_id, shared.title) == ("lab", "Lab ships a new LLM")
    assert [m.source_id for m in shared.mentions] == ["news"]

    [stored_shared] = [
        i for i in store.items_first_seen_between(NOW - DAY, NOW + DAY) if i.id == shared.id
    ]
    assert [m.source_id for m in stored_shared.mentions] == ["news"]


def test_a_second_run_finds_nothing_new_and_health_tracks_the_latest_run() -> None:
    router, store, cfg = make_router(), Store(), crawl_config()
    crawl(cfg, store, router)
    later = NOW + timedelta(hours=1)
    second = crawl(cfg, store, router, now=later)

    assert second.new_items == 0
    assert all(r.new == 0 for r in second.results)
    health = {h["source_id"]: h for h in store.source_health(NOW - DAY, NOW + DAY)}
    assert health["lab"]["ok"] is True
    assert health["broken"]["ok"] is False and "HTTP 500" in health["broken"]["error"]
    assert store.stats()["runs"] == 2


def test_dry_runs_store_nothing() -> None:
    store = Store()
    report = crawl(crawl_config(), store, make_router(), dry_run=True)
    assert report.new_items == 2
    assert store.known_ids() == set()
    assert store.stats()["runs"] == 0


def test_only_selects_sources_even_disabled_ones_and_rejects_unknown_ids() -> None:
    router = make_router()
    report = crawl(crawl_config(), Store(), router, only=["lab"])
    assert [r.source_id for r in report.results] == ["lab"]
    assert router.hits("news.test") == []

    forced = crawl(crawl_config(), Store(), router, only=["off"])
    assert forced.results[0].ok is False  # it ran (and the fake internet 404s it)

    with pytest.raises(ConfigError, match="unknown source id"):
        crawl(crawl_config(), Store(), router, only=["nope"])


def test_a_crashing_or_hanging_source_cannot_sink_the_run(monkeypatch) -> None:
    class Exploding(Source):
        type = "exploding"

        async def fetch(self):
            raise RuntimeError("kaboom")

    class Hanging(Source):
        type = "hanging"

        async def fetch(self):
            await asyncio.sleep(5)
            return []

    monkeypatch.setitem(REGISTRY, "exploding", Exploding)
    monkeypatch.setitem(REGISTRY, "hanging", Hanging)
    cfg = app_config(
        [
            {"id": "boom", "type": "exploding"},
            {"id": "slow", "type": "hanging"},
            {"id": "lab", "type": "rss", "url": "https://lab.test/feed.xml"},
        ],
        source_timeout=0.05,
    )
    report = crawl(cfg, Store(), make_router())
    boom, slow, lab = report.results
    assert boom.error == "kaboom"
    assert slow.error == "timed out"
    assert lab.ok and lab.kept == 2


def test_skip_leaves_sources_out_and_rejects_unknown_ids() -> None:
    router = make_router()
    report = crawl(crawl_config(), Store(), router, skip=["news", "broken"])
    assert [r.source_id for r in report.results] == ["lab"]
    assert router.hits("news.test") == [] and router.hits("broken.test") == []

    with pytest.raises(ConfigError, match="unknown source id"):
        crawl(crawl_config(), Store(), router, skip=["nope"])
