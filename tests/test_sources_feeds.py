from datetime import UTC, datetime
from urllib.parse import quote
from xml.sax.saxutils import escape

import pytest

from ai_radar.http import FetchError
from helpers import Router, fetch, rss


def test_plain_rss_is_cleaned_and_sorted_newest_first() -> None:
    feed = rss(
        {
            "title": "OpenAI ships GPT-5.5",
            "link": "https://blog.test/gpt-5-5?utm_source=rss",
            "date": "Mon, 09 Nov 2026 20:00:00 GMT",
            "desc": "<p>Big <b>news</b> today.</p> The post OpenAI ships GPT-5.5 appeared first on Blog.",
            "categories": ["llm", "release"],
        },
        {
            "title": "Old post",
            "link": "https://blog.test/old",
            "date": "Tue, 03 Nov 2026 10:00:00 GMT",
        },
        {"title": "Undated", "link": "https://blog.test/undated"},
    )
    router = Router().add("feed.test", text=feed)
    items, warnings = fetch(router, "rss", url="https://feed.test/rss.xml")

    assert [i.title for i in items] == ["Undated", "OpenAI ships GPT-5.5", "Old post"]
    post = items[1]
    assert (
        post.url == "https://blog.test/gpt-5-5?utm_source=rss"
    )  # cleaning happens in the pipeline
    assert post.summary == "Big news today."
    assert post.published_at == datetime(2026, 11, 9, 20, 0, tzinfo=UTC)
    assert post.tags == ["llm", "release"]
    assert warnings == []


def test_atom_feeds_work_too() -> None:
    atom = (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>T</title>'
        '<entry><title>Atom post</title><link href="https://blog.test/a"/>'
        "<updated>2026-11-09T10:00:00Z</updated><summary>Short summary</summary>"
        "<author><name>Ada</name></author></entry></feed>"
    )
    items, _ = fetch(Router().add("feed.test", text=atom), "rss", url="https://feed.test/atom")
    assert (items[0].title, items[0].author, items[0].summary) == (
        "Atom post",
        "Ada",
        "Short summary",
    )


def test_an_html_page_is_not_a_feed() -> None:
    router = Router().add("feed.test", text="<html><body>Sign in to continue</body></html>")
    with pytest.raises(FetchError, match="not a valid feed"):
        fetch(router, "rss", url="https://feed.test/rss.xml")


def test_http_errors_surface_as_fetch_errors() -> None:
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch(Router(), "rss", url="https://feed.test/missing.xml")


def test_lobsters_keeps_the_article_and_the_thread() -> None:
    feed = rss(
        {
            "title": "Catching prompt injections",
            "link": "https://blog.test/article",
            "comments": "https://lobste.rs/s/abc/catching_prompt_injections",
            "date": "Sat, 07 Nov 2026 13:34:01 -0600",
        },
        {
            "title": "A text post",
            "link": "https://lobste.rs/s/def/a_text_post",
            "comments": "https://lobste.rs/s/def/a_text_post",
        },
    )
    router = Router().add("lobste.rs/t/ai.rss", text=feed)
    items, _ = fetch(router, "rss", flavor="lobsters", url="https://lobste.rs/t/ai.rss")
    by_title = {i.title: i for i in items}
    assert (
        by_title["Catching prompt injections"].discussion_url
        == "https://lobste.rs/s/abc/catching_prompt_injections"
    )
    assert by_title["A text post"].discussion_url is None  # the thread is the item


def reddit_feed(*entries: dict) -> str:
    body = ""
    for e in entries:
        inner = (
            f'<div class="md"><p>{e["body"]}</p></div> submitted by '
            f'<a href="https://www.reddit.com/user/{e["user"]}"> /u/{e["user"]} </a> to '
            f'<a href="https://www.reddit.com/r/{e["sub"]}/"> r/{e["sub"]} </a> <br/> '
            f'<span><a href="{e["target"]}">[link]</a></span> '
            f'<span><a href="{e["thread"]}">[comments]</a></span>'
        )
        body += (
            f"<entry><author><name>/u/{e['user']}</name></author>"
            f'<category term="{e["sub"]}" label="r/{e["sub"]}"/>'
            f'<content type="html">{escape(inner)}</content><id>t3_{e["id"]}</id>'
            f'<link href="{e["thread"]}"/><updated>2026-11-09T20:00:00+00:00</updated>'
            f"<published>2026-11-09T20:00:00+00:00</published><title>{escape(e['title'])}</title></entry>"
        )
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">{body}</feed>'


def test_reddit_posts_link_to_the_target_and_keep_rank_and_subreddit() -> None:
    thread1 = "https://www.reddit.com/r/LocalLLaMA/comments/abc/cool_model/"
    thread2 = "https://www.reddit.com/r/OpenAI/comments/def/question/"
    feed = reddit_feed(
        {
            "id": "abc",
            "title": "Cool model",
            "body": "Body text",
            "user": "alice",
            "sub": "LocalLLaMA",
            "target": "https://example.com/article",
            "thread": thread1,
        },
        {
            "id": "def",
            "title": "A question",
            "body": "Why though",
            "user": "bob",
            "sub": "OpenAI",
            "target": thread2,
            "thread": thread2,
        },
    )
    router = Router().add("reddit.com", text=feed)
    items, _ = fetch(router, "rss", flavor="reddit", url="https://www.reddit.com/r/a+b/top/.rss")

    link_post, self_post = items  # the feed order (rank) is preserved
    assert link_post.url == "https://example.com/article"
    assert link_post.discussion_url == thread1
    assert (
        link_post.summary == "Body text"
    )  # the "submitted by ... [link] [comments]" footer is gone
    assert link_post.publisher == "r/LocalLLaMA"
    assert (link_post.metrics, link_post.engagement) == ({"reddit_rank": 1.0}, 200.0)
    assert self_post.url == thread2
    assert self_post.discussion_url is None
    assert self_post.engagement == 100.0


def test_product_hunt_titles_carry_the_tagline() -> None:
    content = escape(
        '<p> One URL for every MCP server. </p> <p> <a href="https://www.producthunt.com/products/x">'
        'Discussion</a> | <a href="https://www.producthunt.com/r/p/1">Link</a> </p>'
    )
    atom = (
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>PH</title>'
        "<entry><id>x</id><published>2026-11-06T05:13:56-08:00</published>"
        "<updated>2026-11-06T05:13:56-08:00</updated>"
        '<link rel="alternate" type="text/html" href="https://www.producthunt.com/products/x"/>'
        f'<title>MCP1</title><content type="html">{content}</content></entry></feed>'
    )
    items, _ = fetch(
        Router().add("producthunt", text=atom),
        "rss",
        flavor="producthunt",
        url="https://www.producthunt.com/feed",
    )
    assert items[0].title == "MCP1 — One URL for every MCP server."
    assert items[0].summary == "One URL for every MCP server."


def release_feed(repo: str, *entries: tuple[str, str, str, str]) -> str:
    """entries: (release title, tag, updated, notes)"""
    body = "".join(
        f"<entry><id>tag:github.com,2008:Repository/1/{quote(tag, safe='')}</id>"
        f"<updated>{updated}</updated>"
        f'<link rel="alternate" type="text/html" href="https://github.com/{repo}/releases/tag/{quote(tag, safe="")}"/>'
        f'<title>{escape(title)}</title><content type="html">{escape(notes)}</content></entry>'
        for title, tag, updated, notes in entries
    )
    return f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>Release notes</title>{body}</feed>'


def test_github_releases_skip_prereleases_and_ci_builds() -> None:
    feed = release_feed(
        "acme/tool",
        ("v1.3.0", "v1.3.0", "2026-11-09T10:00:00Z", "<p>Faster inference</p>"),
        ("v1.4.0-rc1", "v1.4.0-rc1", "2026-11-09T11:00:00Z", "rc"),
        ("trunk build", "trunk/abc123", "2026-11-09T12:00:00Z", "ci"),
        ("Tool 1.2.0 — big release", "v1.2.0", "2026-11-05T10:00:00Z", "Notes"),
        ("v1.1.0", "v1.1.0", "2026-11-01T10:00:00Z", "Old"),
    )
    router = Router().add("acme/tool/releases.atom", text=feed)
    items, warnings = fetch(router, "github_releases", repos=["acme/tool"], max_per_repo=2)

    assert [i.title for i in items] == [
        "tool v1.3.0",
        "Tool 1.2.0 — big release",
    ]  # name added once
    assert items[0].summary == "Faster inference"
    assert items[0].url == "https://github.com/acme/tool/releases/tag/v1.3.0"
    assert items[0].tags == ["acme/tool"]
    assert warnings == []


@pytest.mark.parametrize(
    "tag",
    [
        "v2.0.0-beta.1",
        "v1.4.0-rc1",
        "v0.34.1rc0",  # Python style: the word is glued to the number
        "1.0.0rc5",
        "2.0.0b3",
        "1.0.0a1",
        "4.5.0.dev0",
        "nightly-2026-11-24",
        "ciflow/trunk/12345",
    ],
)
def test_pre_release_tags_are_skipped_whatever_the_spelling(tag: str) -> None:
    feed = release_feed("acme/tool", (tag, tag, "2026-11-09T10:00:00Z", "Notes"))
    router = Router().add("acme/tool/releases.atom", text=feed)
    items, _ = fetch(router, "github_releases", repos=["acme/tool"])
    assert items == []


@pytest.mark.parametrize(
    "tag",
    [
        "v1.3.0",
        "b4567",  # a build number, not a beta
        "v2026.11.24",
        "v1.0.0.post1",
        "@ai-sdk/xai@5.0.49",
        "sdk==0.4.14",
        "prefix1",
    ],
)
def test_ordinary_release_tags_are_not_mistaken_for_pre_releases(tag: str) -> None:
    feed = release_feed("acme/tool", (tag, tag, "2026-11-09T10:00:00Z", "Notes"))
    router = Router().add("acme/tool/releases.atom", text=feed)
    items, _ = fetch(router, "github_releases", repos=["acme/tool"])
    assert len(items) == 1


def test_github_releases_survive_one_broken_repo_but_not_all() -> None:
    feed = release_feed("acme/tool", ("v1.0.0", "v1.0.0", "2026-11-09T10:00:00Z", "Notes"))
    router = Router().add("acme/tool/releases.atom", text=feed)
    items, warnings = fetch(router, "github_releases", repos=["acme/tool", "acme/gone"])
    assert len(items) == 1
    assert any("acme/gone" in w for w in warnings)

    with pytest.raises(FetchError, match="all 2 release feeds failed"):
        fetch(Router(), "github_releases", repos=["acme/a", "acme/b"])


def test_releases_can_include_prereleases_when_asked() -> None:
    feed = release_feed(
        "acme/tool", ("v2.0.0-beta.1", "v2.0.0-beta.1", "2026-11-09T10:00:00Z", "beta")
    )
    router = Router().add("acme/tool/releases.atom", text=feed)
    items, _ = fetch(router, "github_releases", repos=["acme/tool"], skip_prereleases=False)
    assert len(items) == 1
