from datetime import UTC, datetime

import pytest
from lxml import etree

from ai_radar.http import FetchError
from ai_radar.sources._page import parse_page_meta, strip_site_suffix
from ai_radar.sources.sitemap import parse_sitemap
from ai_radar.urls import canonicalize_url, item_id
from helpers import Router, fetch, html_page

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://lab.test/news/launch</loc><lastmod>2026-11-09T10:00:00.000Z</lastmod></url>
<url><loc>https://lab.test/news/old-post</loc><lastmod>2026-10-01T10:00:00.000Z</lastmod></url>
<url><loc>https://lab.test/news/category-hub</loc><lastmod>2026-11-10T00:00:00.000Z</lastmod></url>
<url><loc>https://lab.test/news/known-post</loc><lastmod>2026-11-09T11:00:00.000Z</lastmod></url>
<url><loc>https://lab.test/about</loc><lastmod>2026-11-09T10:00:00.000Z</lastmod></url>
</urlset>"""


def lab_router() -> Router:
    return (
        Router()
        .add("/sitemap.xml", text=SITEMAP)
        .add(
            "/news/launch",
            text=html_page(
                title="Big launch | Lab",
                description="We shipped.",
                published="2026-11-09T10:00:00Z",
            ),
        )
        .add("/news/category-hub", text=html_page(title="Category"))
    )


def test_sitemap_reads_new_pages_and_their_metadata() -> None:
    router = lab_router()
    known = {item_id(canonicalize_url("https://lab.test/news/known-post"))}
    items, warnings = fetch(
        router,
        "sitemap",
        name="Lab",
        url="https://lab.test/sitemap.xml",
        include=r"^https://lab\.test/news/[^/]+$",
        require_date=True,
        known_ids=known,
    )
    assert [i.title for i in items] == ["Big launch"]  # " | Lab" is the site's own name
    assert items[0].summary == "We shipped."
    assert items[0].published_at == datetime(2026, 11, 9, 10, 0, tzinfo=UTC)
    assert warnings == []
    assert router.hits("known-post") == []  # already in the store: never fetched again
    assert router.hits("old-post") == []  # older than the age window
    assert router.hits("/about") == []  # does not match `include`
    assert len(router.hits("category-hub")) == 1  # fetched, then dropped for lacking a date


def test_sitemap_without_require_date_keeps_undated_pages() -> None:
    items, _ = fetch(
        lab_router(),
        "sitemap",
        name="Lab",
        url="https://lab.test/sitemap.xml",
        include=r"^https://lab\.test/news/[^/]+$",
        exclude=r"known-post",
    )
    assert sorted(i.title for i in items) == ["Big launch", "Category"]
    hub = next(i for i in items if i.title == "Category")
    assert hub.published_at is not None  # falls back to the sitemap's lastmod


def test_sitemap_can_take_dates_from_the_url() -> None:
    sitemap = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://docs.test/news/news261017</loc></url>"
        "<url><loc>https://docs.test/news/news260101</loc></url></urlset>"
    )
    router = (
        Router()
        .add("/sitemap.xml", text=sitemap)
        .add("/news/news261017", text=html_page(title="V4 release | Docs"))
    )
    items, _ = fetch(
        router,
        "sitemap",
        url="https://docs.test/sitemap.xml",
        max_age_days=30,
        include=r"/news/news\d{6}$",
        date_regex=r"news(\d{6})$",
        date_format="%y%m%d",
    )
    assert [i.title for i in items] == ["V4 release"]
    assert items[0].published_at == datetime(2026, 10, 17, tzinfo=UTC)
    assert router.hits("news260101") == []


def test_sitemap_index_follows_matching_children_only() -> None:
    index = (
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>https://lab.test/sitemap-a.xml</loc></sitemap>"
        "<sitemap><loc>https://lab.test/sitemap-b.xml</loc></sitemap></sitemapindex>"
    )
    child = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://lab.test/news/launch</loc><lastmod>2026-11-09T10:00:00Z</lastmod></url></urlset>"
    )
    router = (
        Router()
        .add("/sitemap-index.xml", text=index)
        .add("/sitemap-a.xml", text=child)
        .add("/news/launch", text=html_page(title="Launch", published="2026-11-09T10:00:00Z"))
    )
    items, _ = fetch(
        router,
        "sitemap",
        url="https://lab.test/sitemap-index.xml",
        include=r"/news/",
        child_include=r"sitemap-a",
    )
    assert len(items) == 1
    assert router.hits("sitemap-b") == []


def test_sitemap_complains_when_nothing_matches_or_pages_all_fail_or_robots_blocks() -> None:
    options = {"url": "https://lab.test/sitemap.xml", "include": r"^https://lab\.test/news/[^/]+$"}
    with pytest.raises(FetchError, match="no sitemap URL matches"):
        fetch(
            Router().add("/sitemap.xml", text=SITEMAP),
            "sitemap",
            url=options["url"],
            include=r"/nothing/",
        )
    with pytest.raises(
        FetchError, match="no sitemap URL matches"
    ):  # an HTML error page, not a sitemap
        fetch(
            Router().add("/sitemap.xml", text="<html><body>Oops</body></html>"),
            "sitemap",
            **options,
        )

    pages_down = Router().add("/sitemap.xml", text=SITEMAP).add("lab.test/news", status=500)
    with pytest.raises(FetchError, match="page fetches failed"):
        fetch(pages_down, "sitemap", exclude="known-post|old-post|hub", **options)

    blocked = (
        Router()
        .add("/robots.txt", text="User-agent: *\nDisallow: /news/\n")
        .add("/sitemap.xml", text=SITEMAP)
    )
    with pytest.raises(FetchError, match="robots"):
        fetch(blocked, "sitemap", **options)
    assert blocked.hits("/news/launch") == []  # never requested


def test_one_bad_page_is_only_a_warning() -> None:
    router = (
        Router()
        .add("/sitemap.xml", text=SITEMAP)
        .add("/news/launch", text=html_page(title="Launch", published="2026-11-09T10:00:00Z"))
        .add("/news/known-post", status=500)
    )
    items, warnings = fetch(
        router,
        "sitemap",
        url="https://lab.test/sitemap.xml",
        include=r"^https://lab\.test/news/[^/]+$",
        exclude="category-hub",
    )
    assert [i.title for i in items] == ["Launch"]
    assert len(warnings) == 1 and "known-post" in warnings[0]


def test_parse_sitemap_ignores_entities_and_namespaces() -> None:
    xml = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        b"<url><loc>https://a.test/&e;</loc></url><url><loc>https://a.test/ok</loc></url></urlset>"
    )
    try:
        urls, children = parse_sitemap(xml)
    except (etree.XMLSyntaxError, ValueError):
        return  # refusing the document outright is just as safe
    assert children == []
    assert all("root:" not in loc for loc, _ in urls)  # the entity is never expanded


LISTING = """<html><body>
<a href="/blog/post-one/">One</a> <a href="/blog/post-one/#comments">same page</a>
<a href="/blog/page/2/">older posts</a> <a href="https://other.test/x">elsewhere</a>
<a href="/blog/post-two">Two</a> <a href="/about">About</a>
</body></html>"""


def test_html_list_follows_matching_links_in_page_order() -> None:
    router = (
        Router()
        .add("/blog/post-one", text=html_page(title="Post one"))
        .add("/blog/post-two", text=html_page(title="Post two"))
        .add("site.test/blog/", text=LISTING)
    )
    options = {
        "url": "https://site.test/blog/",
        "link_regex": r"^https://site\.test/blog/(?!page/)[^/?#]+/?$",
    }
    items, _ = fetch(router, "html_list", **options)
    assert [i.title for i in items] == ["Post one", "Post two"]

    router.requests.clear()
    items, _ = fetch(router, "html_list", max_pages=1, **options)
    assert [i.title for i in items] == ["Post one"]

    known = {item_id(canonicalize_url("https://site.test/blog/post-one/"))}
    items, _ = fetch(router, "html_list", known_ids=known, **options)
    assert [i.title for i in items] == ["Post two"]


def test_html_list_complains_when_no_link_matches() -> None:
    router = Router().add("site.test/blog/", text=LISTING)
    with pytest.raises(FetchError, match="no link on the page matches"):
        fetch(router, "html_list", url="https://site.test/blog/", link_regex=r"/nothing/")


# ---- page metadata -----------------------------------------------------------------------


def test_page_meta_prefers_opengraph() -> None:
    meta = parse_page_meta(
        html_page(title="Hello", description="About it", published="2026-11-09T10:00:00Z")
    )
    assert (meta.title, meta.description) == ("Hello", "About it")
    assert meta.published_at == datetime(2026, 11, 9, 10, 0, tzinfo=UTC)


def test_page_meta_falls_back_to_json_ld_then_time_tags() -> None:
    ld = '<script type="application/ld+json">{"@graph": [{"@type": "Article", "datePublished": "2026-11-08T09:00:00Z"}]}</script>'
    assert parse_page_meta(html_page(extra_head=ld)).published_at == datetime(
        2026, 11, 8, 9, 0, tzinfo=UTC
    )
    page = "<html><head><title>T</title></head><body><time datetime='2026-11-07'>x</time></body></html>"
    assert parse_page_meta(page).published_at == datetime(2026, 11, 7, tzinfo=UTC)
    assert parse_page_meta("<html><head><title>T</title></head></html>").published_at is None


def test_page_meta_author_ignores_profile_urls() -> None:
    profile = '<meta name="author" content="https://facebook.com/someone">'
    named = '<meta name="author" content="Jane Doe">'
    assert parse_page_meta(html_page(extra_head=profile)).author is None
    assert parse_page_meta(html_page(extra_head=named)).author == "Jane Doe"


@pytest.mark.parametrize(
    ("title", "brands", "expected"),
    [
        ("Big news | Acme", ["Acme Inc"], "Big news"),
        ("Big news - Acme Blog", ["acme"], "Big news"),
        ("Introducing Claude \\ Anthropic", ["Anthropic"], "Introducing Claude"),
        ("Using A - B models", ["Acme"], "Using A - B models"),
        ("How AI works | The Verge", ["Anthropic"], "How AI works | The Verge"),
        ("Plain title", ["Acme"], "Plain title"),
    ],
)
def test_strip_site_suffix(title: str, brands: list[str], expected: str) -> None:
    assert strip_site_suffix(title, brands) == expected
