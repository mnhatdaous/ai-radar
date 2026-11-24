import xml.etree.ElementTree as ET
from datetime import date, timedelta

from ai_radar.config import DigestSettings
from ai_radar.digest import (
    ago,
    coverage_line,
    md_link_title,
    md_text,
    md_url,
    pick_top,
    render_digest,
)
from ai_radar.export import feed_selection, write_atom
from ai_radar.models import Mention
from ai_radar.stories import Story, build_stories
from helpers import NOW, filler_items, make_item, story_item

DAY = date(2026, 11, 10)


def make_many(n: int, **overrides):
    return [
        make_item(
            id=f"{overrides.get('kind', 'news')}-{i}",
            canonical_url=f"https://e.test/{overrides.get('kind', 'news')}/{i}",
            url=f"https://e.test/{overrides.get('kind', 'news')}/{i}",
            title=f"Title {i}",
            source_id=f"{overrides.get('kind', 'news')}-src-{i}",
            **overrides,
        )
        for i in range(n)
    ]


def digest(items, **kw) -> str:
    settings = kw.pop("settings", DigestSettings())
    return render_digest(items, day=DAY, now=NOW, settings=settings, **kw)


def test_ago() -> None:
    assert ago(NOW, NOW - timedelta(minutes=30)) == "30m ago"
    assert ago(NOW, NOW - timedelta(hours=5)) == "5h ago"
    assert ago(NOW, NOW - timedelta(days=3)) == "3d ago"
    assert ago(NOW, NOW + timedelta(hours=1)) == "1m ago"  # never negative


def test_markdown_escaping_keeps_feed_content_inert() -> None:
    assert (
        md_text("Use [brackets] <b>tags</b> and `code`")
        == "Use \\[brackets\\] \\<b\\>tags\\</b\\> and \\`code\\`"
    )
    assert md_url("https://e.test/a b(c)") == "https://e.test/a%20b%28c%29"
    md = digest([make_item(title="Click [here](http://evil.test) <script>x</script>")])
    assert "[here](http://evil.test)" not in md
    assert "<script>" not in md


def test_link_tooltips_cannot_break_out_of_their_quotes() -> None:
    assert md_link_title('He said "hi" \\ there\nagain') == 'He said \\"hi\\" \\\\ there again'
    assert md_link_title("x" * 300).endswith("…")
    assert len(md_link_title("x" * 300)) <= 121


def test_the_digest_has_a_header_sections_and_correct_nesting() -> None:
    thread = "https://news.ycombinator.com/item?id=9"
    items = [
        make_item(
            id="top",
            canonical_url="https://e.test/top",
            url="https://e.test/top",
            title="Lab announcement",
            kind="blog",
            weight=1.0,
            source_name="Lab",
            published_at=NOW - timedelta(hours=1),
            summary="Top summary",
        ),
        make_item(
            id="n",
            canonical_url="https://e.test/n",
            url="https://e.test/n",
            title="Coverage",
            summary="Some summary",
            source_name="Outlet",
            topics=["llm", "business"],
            discussion_url=thread,
            metrics={"hn_points": 120, "hn_comments": 40},
            mentions=[Mention("reddit", "Reddit", "https://reddit.com/r/x/1")],
        ),
        make_item(
            id="r",
            canonical_url="https://e.test/r",
            url="https://e.test/r",
            title="Fresh repo",
            kind="repo",
            source_name="GitHub",
            published_at=None,
            metrics={"stars": 1200, "stars_today": 300},
        ),
    ]
    md = digest(
        items,
        settings=DigestSettings(top_n=1, top_mix={"editorial": 1, "community": 0, "builders": 0}),
    )
    lines = md.splitlines()

    assert lines[0] == "# AI Radar · 2026-11-10"
    assert "**3 new items**" in lines[2]
    assert "grouped into" not in lines[2]  # nothing was folded together
    assert "1. **[Lab announcement](https://e.test/top)** — Lab · 1h ago" in md
    assert "   - Top summary" in lines  # nested bullets under "1. " need three spaces
    assert "## News (1)" in md and "## Open-source repos (1)" in md
    assert "- **[Coverage](https://e.test/n)** — Outlet · 2h ago · `llm` `business`" in md
    assert "  - Some summary" in lines  # and two under "- "
    assert (
        "  - HN 120 pts · 40 comments · discussion: [HN](https://news.ycombinator.com/item?id=9)"
        " · also: [Reddit](https://reddit.com/r/x/1)"
    ) in lines
    assert "- **[Fresh repo](https://e.test/r)** — GitHub · new" in md  # undated: no claimed age
    assert "★ 1.2k (+300 today)" in md
    assert "Top stories" in md.split("## News")[0]
    assert "Coverage" not in md.split("## News")[0]  # items in Top stories are not repeated below


def test_empty_days_and_source_health() -> None:
    assert "_Nothing new today._" in digest([])
    health = [
        {"source_id": "bad", "ok": False, "error": "HTTP 500 from bad.test", "warnings": []},
        {"source_id": "meh", "ok": True, "error": None, "warnings": ["page x failed"]},
        {"source_id": "fine", "ok": True, "error": None, "warnings": []},
    ]
    md = digest([make_item()], health=health)
    assert "2 of 3 sources ok" in md
    assert "### Source health" in md
    assert "- `bad` failed: HTTP 500 from bad.test" in md
    assert "- `meh` partly failed: page x failed" in md
    assert "`fine`" not in md
    assert "### Source health" not in digest([make_item()], health=health[2:])


def test_sections_are_capped_and_say_how_much_is_hidden() -> None:
    settings = DigestSettings(top_n=1, top_mix={"editorial": 1}, max_per_section=5)
    md = digest(make_many(20), settings=settings)
    assert "## News (20)" in md
    assert md.count("- **[") == 5
    assert "…and 14 more stories" in md  # 20 stories - 1 in Top stories - 5 shown


def test_top_stories_mix_the_groups_and_cap_each_source() -> None:
    items = (
        make_many(8, kind="news", weight=0.9)
        + make_many(5, kind="repo")
        + make_many(5, kind="discussion")
    )
    settings = DigestSettings(top_n=6, top_mix={"editorial": 3, "community": 2, "builders": 1})
    top = pick_top(build_stories(items, NOW), settings)
    kinds = sorted(s.head.kind for s in top)
    assert kinds == ["discussion", "discussion", "news", "news", "news", "repo"]
    assert [s.score for s in top] == sorted((s.score for s in top), reverse=True)  # best first


def test_top_stories_fill_unused_quota_with_the_best_of_the_rest() -> None:
    items = make_many(2, kind="news") + make_many(6, kind="repo")
    settings = DigestSettings(top_n=6, top_mix={"editorial": 4, "community": 1, "builders": 1})
    top = pick_top(build_stories(items, NOW), settings)
    assert len(top) == 6
    assert sorted(s.head.kind for s in top) == ["news", "news", "repo", "repo", "repo", "repo"]


def test_no_source_can_crowd_the_top() -> None:
    items = [
        make_item(
            id=f"i{n}",
            canonical_url=f"https://e.test/{n}",
            url=f"https://e.test/{n}",
            source_id="one-source",
        )
        for n in range(5)
    ]
    top = pick_top(build_stories(items, NOW), DigestSettings(max_per_source_in_top=2))
    assert len(top) == 2


# ---- stories in the digest ---------------------------------------------------------------

EVENT = [
    story_item(
        "Introducing Orbyx 4.1 and Nimbus 4.1",
        source="lab",
        source_name="Lab",
        kind="blog",
        weight=1.0,
    ),
    story_item(
        "Acme launches new Orbyx and Nimbus 4.1 models",
        source="acme",
        source_name="Acme Wire",
        kind="news",
    ),
    story_item(
        "Orbyx 4.1 and Nimbus 4.1 shake up the coding race",
        source="daily",
        source_name="Daily Planet",
        kind="news",
    ),
    story_item(
        "Orbyx 4.1 just dropped",
        source="reddit",
        source_name="r/Orbyx",
        kind="discussion",
        weight=0.4,
        discussion_url="https://reddit.com/r/orbyx/1",
    ),
]
UNRELATED = story_item("Quarterly earnings beat forecasts at Zentrix", source="markets")


def test_a_story_is_shown_once_with_the_rest_of_its_coverage_listed() -> None:
    md = digest(
        [*filler_items(60), *EVENT, UNRELATED], settings=DigestSettings(max_per_section=100)
    )

    assert "grouped into **" in md.splitlines()[2]
    assert md.count("**[Introducing Orbyx 4.1 and Nimbus 4.1]") == 1
    for folded in (
        "Acme launches new",
        "Orbyx 4.1 and Nimbus 4.1 shake up",
        "Orbyx 4.1 just dropped",
    ):
        assert f"**[{folded}" not in md  # not an entry of its own...
        assert f'"{folded}' in md  # ...but still there: the headline is the link's tooltip

    covered = next(line for line in md.splitlines() if "covered by" in line)
    assert "[Acme Wire](https://acme.test/" in covered
    assert "[Daily Planet](https://daily.test/" in covered
    assert "[r/Orbyx](https://reddit.com/r/orbyx/1 " in covered  # a thread links to the thread
    assert "## Discussions" not in md  # the Reddit thread is part of the story, not listed again
    assert "**[Quarterly earnings beat forecasts at Zentrix]" in md  # unrelated news stays alone
    assert md.splitlines().index(covered) < md.index("## News") or "## News" in md


def test_grouping_can_be_switched_off() -> None:
    settings = DigestSettings(group_stories=False, top_n=0, max_per_section=100)
    md = digest([*filler_items(60), *EVENT, UNRELATED], settings=settings)
    assert "covered by" not in md and "grouped into" not in md
    assert "**[Acme launches new Orbyx and Nimbus 4.1 models](" in md
    assert "**[Orbyx 4.1 just dropped](" in md


def test_the_overlap_setting_controls_how_alike_headlines_must_be() -> None:
    items = [*filler_items(60), *EVENT]
    strict = digest(items, settings=DigestSettings(story_overlap=0.99, max_per_section=100))
    assert "**[Orbyx 4.1 just dropped](" in strict  # no longer similar enough: an entry of its own
    loose = digest(items, settings=DigestSettings(max_per_section=100))
    assert "**[Orbyx 4.1 just dropped](" not in loose  # folded into the story


def test_coverage_line_has_one_link_per_outlet_with_headline_tooltips() -> None:
    head = story_item("Head headline", source="lab")
    a1 = story_item("Acme first take", source="acme", source_name="Acme")
    a2 = story_item("Acme second take", source="acme", source_name="Acme")
    daily = story_item("Daily take", source="daily", source_name="Daily")
    thread = story_item(
        'Reddit says "wow"',
        source="reddit",
        source_name="r/x",
        kind="discussion",
        discussion_url="https://reddit.com/r/x/9",
    )
    story = Story(head=head, score=1.0, others=[a1, a2, daily, thread])

    line = coverage_line(story)
    assert line == (
        'covered by [Acme](https://acme.test/acme-first-take "Acme first take") ×2, '
        '[Daily](https://daily.test/daily-take "Daily take"), '
        '[r/x](https://reddit.com/r/x/9 "Reddit says \\"wow\\"")'
    )
    assert coverage_line(story, limit=1) == (
        'covered by [Acme](https://acme.test/acme-first-take "Acme first take") ×2 +2 more'
    )
    assert coverage_line(Story(head=head, score=1.0)) is None


# ---- feed --------------------------------------------------------------------------------


def test_atom_feed_is_valid_and_safe(tmp_path) -> None:
    items = [
        make_item(
            id="a",
            canonical_url="https://e.test/a",
            url="https://e.test/a?x=1&y=2",
            title="Bad\x0bchar & <tags>",
            summary="Summary \x01 here",
            topics=["llm"],
        ),
        make_item(
            id="b",
            canonical_url="https://e.test/b",
            url="https://e.test/b",
            title="Second",
            published_at=NOW - timedelta(days=1),
        ),
    ]
    path = tmp_path / "feeds" / "ai-radar.atom"
    write_atom(build_stories(items, NOW), path, now=NOW)

    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.parse(path).getroot()  # parses: no illegal characters leaked into the XML
    entries = root.findall("a:entry", ns)
    assert len(entries) == 2
    assert entries[0].find("a:title", ns).text == "Badchar & <tags>"
    assert entries[0].find("a:link", ns).get("href") == "https://e.test/a?x=1&y=2"
    assert [c.get("term") for c in entries[0].findall("a:category", ns)] == ["news", "llm"]
    assert root.find("a:updated", ns).text == "2026-11-10T00:30:00Z"


def test_feed_entries_carry_the_rest_of_the_coverage_as_related_links(tmp_path) -> None:
    stories = feed_selection([*filler_items(60), *EVENT], NOW)
    event = next(s for s in stories if s.others)
    path = tmp_path / "feed.atom"
    write_atom([event], path, now=NOW)

    ns = {"a": "http://www.w3.org/2005/Atom"}
    entry = ET.parse(path).getroot().find("a:entry", ns)
    assert entry.find("a:title", ns).text == "Introducing Orbyx 4.1 and Nimbus 4.1"
    related = [link for link in entry.findall("a:link", ns) if link.get("rel") == "related"]
    assert {link.get("title") for link in related} == {
        "Acme launches new Orbyx and Nimbus 4.1 models",
        "Orbyx 4.1 and Nimbus 4.1 shake up the coding race",
        "Orbyx 4.1 just dropped",
    }


def test_feed_selection_folds_coverage_takes_the_best_then_orders_by_date() -> None:
    strong = make_item(
        id="s", canonical_url="https://e.test/s", weight=1.0, published_at=NOW - timedelta(days=2)
    )
    weak = make_item(
        id="w", canonical_url="https://e.test/w", weight=0.1, published_at=NOW - timedelta(hours=1)
    )
    mid = make_item(
        id="m", canonical_url="https://e.test/m", weight=0.6, published_at=NOW - timedelta(hours=3)
    )
    picked = feed_selection([weak, strong, mid], NOW, limit=2)
    assert [s.head.id for s in picked] == ["m", "s"]  # the weak one is cut; the rest newest first

    folded = feed_selection([*filler_items(60), *EVENT], NOW, limit=100)
    assert len(folded) == 60 + 1  # the four items of the event became one entry
    assert (
        feed_selection(
            [*filler_items(60), *EVENT],
            NOW,
            settings=DigestSettings(group_stories=False),
            limit=100,
        )[0].others
        == []
    )
