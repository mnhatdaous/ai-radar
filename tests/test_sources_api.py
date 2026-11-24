import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ai_radar.http import FetchError
from helpers import NOW, Router, crawl_context, fetch, make_source


def ts(delta: timedelta) -> int:
    return int((NOW - delta).timestamp())


def iso(delta: timedelta) -> str:
    return (NOW - delta).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---- Hacker News -------------------------------------------------------------------------


def hn_payload() -> dict:
    return {
        "hits": [
            {
                "objectID": "1",
                "title": "Show HN: tiny LLM runner",
                "url": "https://github.com/x/y",
                "points": 120,
                "num_comments": 30,
                "created_at_i": ts(timedelta(hours=3)),
                "author": "a",
            },
            {
                "objectID": "2",
                "title": "Ask HN: How do you use AI?",
                "url": None,
                "story_text": "<p>Curious about <b>your</b> setup</p>",
                "points": 50,
                "num_comments": 10,
                "created_at_i": ts(timedelta(hours=5)),
                "author": "b",
            },
        ]
    }


def test_hackernews_items_link_to_the_story_and_the_thread() -> None:
    router = Router().add("hn.algolia.com", json=hn_payload())
    items, warnings = fetch(router, "hackernews", queries=["AI", "LLM"], min_points=25)

    assert [i.title for i in items] == ["Show HN: tiny LLM runner", "Ask HN: How do you use AI?"]
    show, ask = items  # strongest first: 120 + 15 > 50 + 5
    assert show.url == "https://github.com/x/y"
    assert show.discussion_url == "https://news.ycombinator.com/item?id=1"
    assert show.metrics == {"hn_points": 120.0, "hn_comments": 30.0}
    assert show.engagement == 135.0
    assert (
        ask.url == "https://news.ycombinator.com/item?id=2"
    )  # no outside link: the thread is the item
    assert ask.discussion_url is None
    assert ask.summary == "Curious about your setup"
    assert warnings == []


def test_hackernews_asks_algolia_for_recent_popular_title_matches() -> None:
    router = Router().add("hn.algolia.com", json={"hits": []})
    fetch(router, "hackernews", queries=["AI"], min_points=25, hn_tag="show_hn")
    params = router.requests[0].url.params
    since = int((NOW - timedelta(hours=30)).timestamp())
    assert params["tags"] == "show_hn"
    assert params["restrictSearchableAttributes"] == "title"
    assert params["numericFilters"] == f"created_at_i>{since},points>=25"


def test_hackernews_survives_a_failing_query_but_not_all_of_them() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["query"] == "LLM":
            return httpx.Response(500)
        return httpx.Response(200, json=hn_payload())

    items, warnings = fetch(
        Router().add("algolia", handler=handler), "hackernews", queries=["AI", "LLM"]
    )
    assert len(items) == 2
    assert any("LLM" in w for w in warnings)

    with pytest.raises(FetchError, match="all 2 Algolia queries failed"):
        fetch(Router().add("algolia", status=500), "hackernews", queries=["AI", "LLM"])


# ---- arXiv -------------------------------------------------------------------------------

ARXIV = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>arXiv</title>
<entry><id>http://arxiv.org/abs/2611.11497v1</id><updated>2026-11-08T16:50:34Z</updated>
<published>2026-11-08T16:50:34Z</published><title>A study of
  line breaks</title><summary>  An abstract.  </summary>
<author><name>A One</name></author><author><name>B Two</name></author>
<author><name>C Three</name></author><author><name>D Four</name></author>
<link href="http://arxiv.org/abs/2611.11497v1" rel="alternate" type="text/html"/>
<category term="cs.CL" scheme="http://arxiv.org/schemas/atom"/>
<category term="cs.AI" scheme="http://arxiv.org/schemas/atom"/></entry></feed>"""


def test_arxiv_papers_are_tidied_and_requests_are_paced() -> None:
    router = Router().add("export.arxiv.org", text=ARXIV)

    async def go():
        async with crawl_context(router) as ctx:
            source = make_source(ctx, "arxiv", categories=["cs.AI", "cs.CL"], max_results=5)
            items = await source.fetch()
            return items, ctx.http._policy_for("export.arxiv.org")

    items, policy = asyncio.run(go())
    assert [r.url.params["search_query"] for r in router.requests] == ["cat:cs.AI", "cat:cs.CL"]
    assert router.requests[0].url.params["sortBy"] == "submittedDate"
    paper = items[0]
    assert paper.title == "A study of line breaks"
    assert paper.summary == "An abstract."
    assert paper.author == "A One, B Two, C Three et al."
    assert paper.tags == ["cs.CL", "cs.AI"]
    assert paper.published_at == datetime(2026, 11, 8, 16, 50, 34, tzinfo=UTC)
    assert (policy.min_interval, policy.max_concurrency) == (3.1, 1)  # arXiv's one-per-3s rule


def test_arxiv_fails_only_if_every_request_fails() -> None:
    with pytest.raises(FetchError, match="all 2 arXiv requests failed"):
        fetch(Router().add("arxiv", status=500), "arxiv", categories=["cs.AI", "cs.CL"])


# ---- Hugging Face ------------------------------------------------------------------------


def paper(paper_id: str, upvotes: int, daily: str) -> dict:
    return {
        "paper": {
            "id": paper_id,
            "title": f"Paper {paper_id}",
            "summary": "Long abstract.",
            "ai_summary": "Short AI summary.",
            "upvotes": upvotes,
            "publishedAt": "2026-11-05T10:00:00.000Z",
            "submittedOnDailyAt": f"{daily}T00:00:00.000Z",
            "authors": [{"name": n} for n in "ABCD"],
            "ai_keywords": ["k1", "k2"],
        },
        "publishedAt": "2026-11-05T10:00:00.000Z",
        "title": f"Paper {paper_id}",
        "numComments": 3,
    }


def test_hf_daily_papers_read_completed_days_and_rank_by_upvotes() -> None:
    by_day = {
        "2026-11-09": [paper("2611.00002", 2, "2026-11-09"), paper("2611.00001", 64, "2026-11-09")],
        "2026-11-08": [],  # weekend: legitimately empty
        "2026-11-07": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=by_day[request.url.params["date"]])

    router = Router().add("huggingface.co/api/daily_papers", handler=handler)
    items, warnings = fetch(router, "huggingface", mode="daily_papers", days=3)

    assert [r.url.params["date"] for r in router.requests] == [
        "2026-11-09",
        "2026-11-08",
        "2026-11-07",
    ]
    best = items[0]
    assert best.url == "https://arxiv.org/abs/2611.00001"
    assert best.discussion_url == "https://huggingface.co/papers/2611.00001"
    assert best.summary == "Short AI summary."
    assert best.author == "A, B, C et al."
    assert best.published_at == datetime(2026, 11, 9, tzinfo=UTC)  # the day it was featured
    assert (best.engagement, best.metrics["hf_upvotes"]) == (192.0, 64.0)
    assert warnings == []


def test_hf_daily_papers_tolerate_one_failing_day() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["date"] == "2026-11-08":
            return httpx.Response(500)
        return httpx.Response(200, json=[paper("2611.00001", 5, "2026-11-09")])

    items, warnings = fetch(
        Router().add("daily_papers", handler=handler), "huggingface", mode="daily_papers", days=2
    )
    assert len(items) == 1
    assert any("2026-11-08" in w for w in warnings)


@pytest.mark.parametrize(
    ("mode", "prefix"), [("models", ""), ("datasets", "datasets/"), ("spaces", "spaces/")]
)
def test_hf_trending_keeps_only_new_repos(mode: str, prefix: str) -> None:
    payload = [
        {
            "id": "acme/new-thing",
            "likes": 100,
            "downloads": 5000,
            "trendingScore": 250,
            "createdAt": iso(timedelta(days=5)),
            "pipeline_tag": "text-generation",
            "library_name": "transformers",
        },
        {
            "id": "old/classic",
            "likes": 9999,
            "trendingScore": 900,
            "createdAt": "2024-01-01T00:00:00.000Z",
        },
    ]
    router = Router().add(f"huggingface.co/api/{mode}", json=payload)
    items, _ = fetch(router, "huggingface", mode=mode, max_age_days=30)
    assert [i.title for i in items] == ["acme/new-thing"]
    item = items[0]
    assert item.url == f"https://huggingface.co/{prefix}acme/new-thing"
    assert item.engagement == 50.0  # trending score / 5
    assert item.author == "acme"
    assert "100 likes" in item.summary
    assert router.requests[0].url.params["sort"] == "trendingScore"


# ---- OpenRouter --------------------------------------------------------------------------


def test_openrouter_lists_only_recently_added_models() -> None:
    payload = {
        "data": [
            {
                "id": "acme/model-x",
                "name": "Acme: Model X",
                "created": ts(timedelta(days=2)),
                "description": "A fast model.",
                "context_length": 262144,
                "pricing": {"prompt": "0.0000001", "completion": "0.0000004"},
                "architecture": {"modality": "text+image->text"},
            },
            {
                "id": "acme/free-y",
                "name": "Free Y",
                "created": ts(timedelta(days=1)),
                "context_length": 8000,
                "pricing": {"prompt": "0", "completion": "0"},
            },
            {"id": "old/z", "name": "Old", "created": ts(timedelta(days=30))},
        ]
    }
    items, _ = fetch(Router().add("openrouter.ai", json=payload), "openrouter")
    assert [i.title for i in items] == ["Free Y", "Acme: Model X"]  # newest first
    free, model_x = items
    assert "free" in free.summary and "8,000 token context" in free.summary
    assert "262,144 token context" in model_x.summary
    assert "$0.10/$0.40 per M tokens" in model_x.summary
    assert model_x.url == "https://openrouter.ai/acme/model-x"
    assert model_x.tags == ["text+image->text"]


# ---- GitHub ------------------------------------------------------------------------------

REPO = {
    "full_name": "acme/agent",
    "html_url": "https://github.com/acme/agent",
    "description": "Agent toolkit",
    "stargazers_count": 300,
    "forks_count": 10,
    "language": "Python",
    "topics": ["llm", "agents"],
    "created_at": "2026-11-08T10:00:00Z",
    "owner": {"login": "acme"},
}


def test_github_search_asks_for_new_popular_repos_and_dedupes_across_topics() -> None:
    router = Router().add("api.github.com/search/repositories", json={"items": [REPO]})
    items, _ = fetch(router, "github_search", topics=["llm", "mcp"], min_stars=25)

    assert len(items) == 1  # the same repo under two topics
    assert router.requests[0].url.params["q"] == "topic:llm created:>=2026-11-03 stars:>=25"
    assert router.requests[0].url.params["sort"] == "stars"
    repo = items[0]
    assert (repo.title, repo.url, repo.author) == (
        "acme/agent",
        "https://github.com/acme/agent",
        "acme",
    )
    assert repo.metrics == {"stars": 300.0, "forks": 10.0}
    assert repo.engagement == 100.0
    assert repo.tags == ["llm", "agents", "Python"]


def test_github_search_paces_itself_by_whether_it_has_a_token() -> None:
    router = Router().add("api.github.com", json={"items": []})

    async def go(token: str | None):
        async with crawl_context(router, github_token=token) as ctx:
            await make_source(ctx, "github_search", topics=["llm"]).fetch()
            return ctx.http._policy_for("api.github.com").min_interval

    assert asyncio.run(go(None)) == 6.5  # 10 searches a minute
    assert asyncio.run(go("tok")) == 2.2  # 30 searches a minute
    assert router.requests[-1].headers["authorization"] == "Bearer tok"
    assert "authorization" not in router.requests[0].headers


def article(slug: str, description: str, language: str, stars: str, forks: str, today: str) -> str:
    owner, name = slug.split("/")
    return (
        f'<article class="Box-row"><h2 class="h3 lh-condensed"><a href="/{slug}">'
        f' <span class="text-normal">{owner} /</span> {name} </a></h2>'
        f'<p class="col-9 color-fg-muted my-1 pr-4"> {description} </p>'
        '<div class="f6 color-fg-muted mt-2"><span class="d-inline-block ml-0 mr-3">'
        f'<span itemprop="programmingLanguage">{language}</span></span>'
        f'<a class="Link--muted d-inline-block mr-3" href="/{slug}/stargazers"> {stars} </a>'
        f'<a class="Link--muted d-inline-block mr-3" href="/{slug}/forks"> {forks} </a>'
        f'<span class="d-inline-block float-sm-right"> {today} stars today </span></div></article>'
    )


def test_github_trending_page_is_parsed() -> None:
    page = "<html><body>" + article(
        "acme/agent-kit", "Toolkit for building AI agents", "Python", "12,345", "678", "1,038"
    )
    page += article("acme/quiet", "A quiet repo", "Go", "900", "12", "40") + "</body></html>"
    router = Router().add("github.com/trending", text=page)
    items, _ = fetch(router, "github_trending")

    top, quiet = items  # sorted by stars gained today
    assert top.title == "acme/agent-kit"
    assert top.url == "https://github.com/acme/agent-kit"
    assert top.summary == "Toolkit for building AI agents"
    assert top.metrics == {"stars": 12345.0, "forks": 678.0, "stars_today": 1038.0}
    assert top.engagement == 259.5
    assert top.tags == ["Python"]
    assert quiet.title == "acme/quiet"
    assert router.requests[-1].url.params["since"] == "daily"


def test_github_trending_notices_layout_changes_and_robots_blocks() -> None:
    with pytest.raises(FetchError, match="layout"):
        fetch(
            Router().add("github.com/trending", text="<html><body>nothing here</body></html>"),
            "github_trending",
        )
    blocked = Router().add("/robots.txt", text="User-agent: *\nDisallow: /trending\n")
    with pytest.raises(FetchError, match="robots"):
        fetch(blocked, "github_trending")


# ---- GDELT -------------------------------------------------------------------------------


def test_gdelt_articles_are_filtered_and_attributed_to_their_outlet() -> None:
    payload = {
        "articles": [
            {
                "url": "https://www.example.com/a",
                "title": "OpenAI does a thing",
                "seendate": "20261109T203000Z",
                "domain": "www.example.com",
            },
            {
                "url": "https://spam.test/b",
                "title": "Spam about AI",
                "seendate": "20261109T203000Z",
                "domain": "spam.test",
            },
            {
                "url": "https://www.example.com/a",
                "title": "Duplicate url",
                "seendate": "20261109T203000Z",
                "domain": "example.com",
            },
        ]
    }
    router = Router().add("gdeltproject.org", json=payload)

    async def go():
        async with crawl_context(router) as ctx:
            source = make_source(
                ctx, "gdelt", queries=['"artificial intelligence"'], exclude_domains=["spam.test"]
            )
            return await source.fetch(), ctx.http._policy_for("api.gdeltproject.org")

    items, policy = asyncio.run(go())
    assert [i.title for i in items] == ["OpenAI does a thing"]
    assert items[0].publisher == "example.com"
    assert items[0].published_at == datetime(2026, 11, 9, 20, 30, tzinfo=UTC)
    assert (
        router.requests[0].url.params["query"] == '("artificial intelligence") sourcelang:english'
    )
    assert policy.min_interval == 6.0
