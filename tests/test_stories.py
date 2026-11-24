import json
import os
import random
import subprocess
import sys
import time
from datetime import timedelta

import pytest

from ai_radar.scoring import rank
from ai_radar.stories import STORY_KINDS, Story, StoryParams, build_stories, title_tokens
from helpers import NOW, ROOT, filler_items, story_item


def stories_for(*items, fillers: int = 60, **kw) -> list[Story]:
    return build_stories([*filler_items(fillers), *items], NOW, **kw)


def story_of(stories: list[Story], item) -> Story:
    return next(s for s in stories if item.id in {i.id for i in s.items})


def ids(story: Story) -> set[str]:
    return {i.id for i in story.items}


def groups(stories: list[Story]) -> list[tuple[str, ...]]:
    """A canonical fingerprint of the grouping, for comparing runs."""
    return sorted((s.head.id, *sorted(i.id for i in s.others)) for s in stories if s.others)


# ---- tokens ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (
            "Anthropic’s New Claude Fable 5.2 Models",
            {"anthropic", "claude", "fable", "5.2", "model"},
        ),
        ("How to use AI on the web", {"web"}),
        ("Jensen Huang’s affair, explained", {"jensen", "huang", "affair", "explained"}),
        ("llm-anthropic 0.31", {"llm", "anthropic", "0.31"}),
        ("Class status analysis gas bonus", {"class", "status", "analysis", "gas", "bonus"}),
        ("2026 v2 o3 gpt5", {"gpt5"}),  # bare numbers and tiny words say nothing
        ("", set()),
    ],
)
def test_title_tokens(title: str, expected: set[str]) -> None:
    assert title_tokens(title) == expected


# ---- grouping ----------------------------------------------------------------------------

LAB = story_item("Introducing Orbyx 4.1 and Nimbus 4.1", source="lab", kind="blog", weight=1.0)
WIRE = story_item("Acme launches new Orbyx and Nimbus 4.1 models", source="acme")
DAILY = story_item("Orbyx 4.1 and Nimbus 4.1 shake up the coding race", source="daily")
THREAD = story_item("Orbyx 4.1 just dropped", source="reddit", kind="discussion", weight=0.4)
OTHER = story_item("Quarterly earnings beat forecasts at Zentrix", source="markets")


def test_coverage_of_one_event_becomes_one_story() -> None:
    stories = stories_for(LAB, WIRE, DAILY, THREAD, OTHER)
    event = story_of(stories, LAB)
    assert ids(event) == {LAB.id, WIRE.id, DAILY.id, THREAD.id}
    assert event.head is LAB  # the lab's own post: an article, and the most trusted source
    assert len(story_of(stories, OTHER).items) == 1
    assert sum(1 for s in stories if len(s.items) > 1) == 1  # the made-up filler never groups
    assert len(stories) == 60 + 2


def test_the_head_is_an_article_even_when_a_thread_scores_higher() -> None:
    viral = story_item(
        "Orbyx 4.1 just dropped", source="reddit", kind="discussion", weight=0.9, engagement=50000
    )
    event = story_of(stories_for(LAB, WIRE, viral), viral)
    assert event.head.kind in {"news", "blog"}
    assert viral in event.others
    assert event.score >= rank([viral], NOW)[0][0]  # the story is as strong as its strongest item


def test_threads_alone_can_form_a_story() -> None:
    a = story_item("Orbyx 4.1 benchmarks are wild", source="reddit", kind="discussion", weight=0.5)
    b = story_item("Orbyx 4.1 benchmarks reproduced", source="hn", kind="discussion", weight=0.4)
    event = story_of(stories_for(a, b), a)
    assert ids(event) == {a.id, b.id}
    assert event.head is a  # no article: the better-scoring thread heads it


def test_a_thread_cannot_pull_a_group_away_from_its_articles() -> None:
    # A short thread that outscores the articles must not lead: the second article shares only
    # "4.1" with *it*, but "Nimbus 4.1" with the first article.
    thread = story_item(
        "Orbyx 4.1 dropped", source="reddit", kind="discussion", weight=1.0, engagement=9000
    )
    first = story_item("Acme launches Orbyx and Nimbus 4.1 models", source="acme")
    second = story_item("Nimbus 4.1 benchmarks", source="daily")
    event = story_of(stories_for(thread, first, second), first)
    assert ids(event) == {thread.id, first.id, second.id}


def test_one_shared_word_is_never_a_story_however_rare() -> None:
    a = story_item("Orbyx pricing details revealed", source="acme")
    b = story_item("Orbyx recap of the quarter", source="daily")
    stories = stories_for(a, b)
    assert len(story_of(stories, a).items) == 1 and len(story_of(stories, b).items) == 1


def test_min_words_is_a_separate_guard_from_min_shared() -> None:
    a = story_item("Orbyx pricing", source="acme")
    b = story_item("Orbyx recap", source="daily")
    lenient = StoryParams(min_shared=0.5)  # one rare word now clears the weight bar...
    assert len(story_of(stories_for(a, b, params=lenient), a).items) == 1  # ...but not min_words
    assert (
        len(story_of(stories_for(a, b, params=StoryParams(min_shared=0.5, min_words=1)), a).items)
        == 2
    )


def test_common_topic_words_do_not_make_a_story() -> None:
    # "claude" and "code" are everywhere in this pool, so sharing them means little.
    pool = [
        story_item(f"Claude {word} update{i}", source=f"c{i}")
        for i, word in enumerate(["adds"] * 14)
    ] + [
        story_item(f"Code {word}{i} review", source=f"k{i}")
        for i, word in enumerate(["audit"] * 14)
    ]
    x = story_item("Claude Code exposes secrets on GitHub", source="ars")
    y = story_item("JPMorgan broadens Claude Code use beyond engineers", source="unite")
    stories = stories_for(x, y, *pool)
    assert len(story_of(stories, x).items) == 1


def test_groups_do_not_chain() -> None:
    # A~B and B~C, but A and C share one word only: C must not ride in on B.
    a = story_item("Kestrel plover osprey", source="a", published_at=NOW - timedelta(hours=1))
    b = story_item("Plover osprey harrier", source="b", published_at=NOW - timedelta(hours=2))
    c = story_item("Osprey harrier falcon", source="c", published_at=NOW - timedelta(hours=3))
    stories = stories_for(a, b, c)
    assert ids(story_of(stories, a)) == {a.id, b.id}
    assert ids(story_of(stories, c)) == {c.id}


def test_an_item_matching_two_groups_equally_joins_the_better_one() -> None:
    first = story_item(
        "Orbyx Nimbus kestrel osprey harrier", source="a", published_at=NOW - timedelta(hours=1)
    )
    second = story_item(
        "Orbyx Nimbus plover falcon merlin", source="b", published_at=NOW - timedelta(hours=2)
    )
    joiner = story_item("Orbyx Nimbus", source="c", published_at=NOW - timedelta(hours=3))
    stories = stories_for(first, second, joiner)
    assert (
        len(story_of(stories, first).items) == 2
    )  # the first leader was not merged with the second
    assert ids(story_of(stories, joiner)) == {first.id, joiner.id}


def test_only_events_are_grouped_not_artifacts() -> None:
    article = story_item("Orbyx 4.1 and Nimbus 4.1 are here", source="acme")
    artifacts = [
        story_item("Orbyx 4.1 and Nimbus 4.1 are here", source=f"x{kind}", kind=kind)
        for kind in ("paper", "repo", "model", "dataset", "tool", "release")
    ]
    assert not {"paper", "repo", "model", "dataset", "tool", "release"} & STORY_KINDS
    stories = stories_for(article, *artifacts)
    assert all(len(s.items) == 1 for s in stories)


def test_grouping_can_be_turned_off_and_tightened() -> None:
    items = [LAB, WIRE, DAILY, THREAD]
    flat = stories_for(*items, group=False)
    assert all(not s.others for s in flat) and len(flat) == 60 + 4

    strict = stories_for(*items, params=StoryParams(min_overlap=0.99))
    assert len(story_of(strict, THREAD).items) == 1  # two of its three words are not enough now
    assert len(story_of(strict, LAB).items) == 3


def test_empty_and_artifact_only_inputs() -> None:
    assert build_stories([], NOW) == []
    repo = story_item("A repo", source="gh", kind="repo")
    assert [s.head for s in build_stories([repo], NOW)] == [repo]


# ---- ranking -----------------------------------------------------------------------------


def test_extra_outlets_raise_a_story_s_score_up_to_a_cap() -> None:
    def outlets(n: int, prefix: str) -> list:
        return [
            story_item(f"Orbyx 4.1 Nimbus 4.1 take{prefix}{i}", source=f"{prefix}{i}")
            for i in range(n)
        ]

    lone = story_item("Orbyx 4.1 Nimbus 4.1 solo", source="solo")
    base = rank([lone], NOW)[0][0]
    assert story_of(stories_for(lone), lone).score == pytest.approx(base)

    three = outlets(3, "a")
    s3 = story_of(stories_for(*three), three[0])
    assert s3.score == pytest.approx(rank(three, NOW)[0][0] + 0.8 * 2)

    nine = outlets(9, "b")
    s9 = story_of(stories_for(*nine), nine[0])
    assert s9.score == pytest.approx(
        rank(nine, NOW)[0][0] + 0.8 * 5
    )  # capped at five extra outlets


def test_several_items_from_one_outlet_earn_no_bonus() -> None:
    same = [story_item(f"Orbyx 4.1 Nimbus 4.1 take{i}", source="one") for i in range(4)]
    event = story_of(stories_for(*same), same[0])
    assert len(event.items) == 4 and event.outlets == 1
    assert event.score == pytest.approx(rank(same, NOW)[0][0])


def test_stories_are_ordered_best_first_and_a_widely_covered_one_leads() -> None:
    quiet = story_item("Zentrix posts earnings", source="markets", weight=0.7)
    stories = stories_for(LAB, WIRE, DAILY, THREAD, quiet)
    assert [s.score for s in stories] == sorted((s.score for s in stories), reverse=True)
    assert stories[0].head is LAB


def test_coverage_lists_each_outlet_once_best_first_with_a_count() -> None:
    head = story_item("Head", source="lab")
    a1 = story_item("Acme first", source="acme", source_name="Acme")
    a2 = story_item("Acme second", source="acme", source_name="Acme")
    d = story_item("Daily take", source="daily", source_name="Daily")
    story = Story(head=head, score=1.0, others=[a1, d, a2])
    assert story.coverage() == [(a1, 2), (d, 1)]
    assert story.items == [head, a1, d, a2]
    assert story.outlets == 3


# ---- determinism -------------------------------------------------------------------------


def test_the_input_order_does_not_matter() -> None:
    items = [*filler_items(60), LAB, WIRE, DAILY, THREAD, OTHER]
    expected = groups(build_stories(items, NOW))
    for seed in range(5):
        shuffled = items[:]
        random.Random(seed).shuffle(shuffled)
        assert groups(build_stories(shuffled, NOW)) == expected


_SCRIPT = """
import json, sys
sys.path.insert(0, {tests!r})
from helpers import NOW, filler_items, story_item
from ai_radar.stories import build_stories
titles = [
    "Orbyx Nimbus kestrel osprey harrier", "Orbyx Nimbus plover falcon merlin", "Orbyx Nimbus",
    "Introducing Orbyx 4.1 and Nimbus 4.1", "Acme launches new Orbyx and Nimbus 4.1 models",
    "Kestrel plover osprey", "Plover osprey harrier", "Osprey harrier falcon",
]
items = filler_items(60) + [story_item(t, source=f"s{{i}}") for i, t in enumerate(titles)]
stories = build_stories(items, NOW)
print(json.dumps(sorted([s.head.id] + sorted(i.id for i in s.others) for s in stories if s.others)))
"""


def test_the_result_does_not_depend_on_pythons_hash_seed() -> None:
    outputs = set()
    for seed in ("1", "2", "3", "99"):
        result = subprocess.run(
            [sys.executable, "-c", _SCRIPT.format(tests=str(ROOT / "tests"))],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.add(result.stdout.strip())
    assert len(outputs) == 1
    assert json.loads(outputs.pop())  # and something was actually grouped


# ---- scale -------------------------------------------------------------------------------


def test_thousands_of_headlines_group_quickly() -> None:
    rng = random.Random(0)
    vocab = [f"word{n}x" for n in range(600)]
    items = [
        story_item(
            " ".join(rng.sample(vocab, 6)),
            source=f"s{i % 40}",
            kind="news" if i % 5 else "discussion",
        )
        for i in range(3000)
    ]
    started = time.perf_counter()
    stories = build_stories(items, NOW)
    elapsed = time.perf_counter() - started
    assert len(stories) <= len(items)
    assert elapsed < 20, f"grouping 3000 headlines took {elapsed:.1f}s"
