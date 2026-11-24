import pytest

from ai_radar.urls import canonicalize_url, clean_url, domain_of, item_id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Example.com/a/?utm_source=x&id=3&fbclid=abc#frag", "https://example.com/a?id=3"),
        ("http://www.example.com/post/", "https://example.com/post"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/a//b///c", "https://example.com/a/b/c"),
        ("https://example.com/blog/index.html", "https://example.com/blog"),
        ("//example.com/x", "https://example.com/x"),
        ("https://example.com/p?b=2&a=1", "https://example.com/p?a=1&b=2"),
        ("https://medium.com/p/x?source=rss-123", "https://medium.com/p/x"),
        ("https://example.com/p?source=homepage", "https://example.com/p?source=homepage"),
        ("https://news.ycombinator.com/item?id=42&p=2", "https://news.ycombinator.com/item?id=42"),
        ("https://old.reddit.com/r/x/comments/1/t/", "https://reddit.com/r/x/comments/1/t"),
        ("https://www.reddit.com/r/x/comments/1/t/", "https://reddit.com/r/x/comments/1/t"),
    ],
)
def test_canonicalize(raw: str, expected: str) -> None:
    assert canonicalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://arxiv.org/pdf/2611.11497v2.pdf",
        "http://arxiv.org/abs/2611.11497v1",
        "https://arxiv.org/abs/2611.11497",
        "https://export.arxiv.org/abs/2611.11497v3",
        "https://huggingface.co/papers/2611.11497",
        "https://huggingface.co/papers/2611.11497v2",
    ],
)
def test_arxiv_and_hf_paper_links_collapse(raw: str) -> None:
    assert canonicalize_url(raw) == "https://arxiv.org/abs/2611.11497"


def test_old_style_arxiv_ids() -> None:
    assert (
        canonicalize_url("https://arxiv.org/abs/cs/0601001v2") == "https://arxiv.org/abs/cs/0601001"
    )


def test_github_owner_and_repo_are_case_insensitive() -> None:
    assert canonicalize_url("https://GitHub.com/Foo/Bar.git") == "https://github.com/foo/bar"
    deep = canonicalize_url("https://github.com/Foo/Bar/releases/tag/V1.0")
    assert deep == "https://github.com/foo/bar/releases/tag/V1.0"  # tags are case-sensitive


def test_youtube_links_collapse_to_the_video() -> None:
    expected = "https://youtube.com/watch?v=abc123"
    assert canonicalize_url("https://youtu.be/abc123?si=zzz") == expected
    assert canonicalize_url("https://www.youtube.com/watch?v=abc123&feature=share") == expected


def test_twitter_becomes_x() -> None:
    assert canonicalize_url("https://twitter.com/user/status/1").startswith("https://x.com/user")


@pytest.mark.parametrize(
    "raw",
    [
        "javascript:alert(1)",
        "ftp://example.com/a",
        "mailto:a@b.c",
        "",
        "not a url",
        "data:text/html,x",
    ],
)
def test_rejects_non_http_urls(raw: str) -> None:
    with pytest.raises(ValueError):
        canonicalize_url(raw)
    with pytest.raises(ValueError):
        clean_url(raw)


def test_clean_url_only_removes_tracking_and_fragment() -> None:
    assert (
        clean_url("http://www.example.com/Post/?utm_x=1&a=2#top")
        == "http://www.example.com/Post/?a=2"
    )
    assert clean_url("https://arxiv.org/pdf/2611.11497v2.pdf") == "https://arxiv.org/abs/2611.11497"


def test_item_id_is_stable_and_distinct() -> None:
    a, b = item_id("https://example.com/a"), item_id("https://example.com/b")
    assert a == item_id("https://example.com/a")
    assert a != b
    assert len(a) == 16


def test_domain_of() -> None:
    assert domain_of("https://www.Example.com/x") == "example.com"
    assert domain_of("not a url") == ""
