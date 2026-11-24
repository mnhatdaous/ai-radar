"""Group coverage of the same event, so one story is shown once.

Outlets cover the same news within hours, each under its own URL, so URL dedupe cannot see it.
Headlines can: they repeat the names and numbers that make the event what it is ("Fable",
"Mythos", "5.2"), and those words are rare among a day's headlines. Items are grouped when they
share enough rare headline words, each weighted by how rare it is.

Grouping is conservative on purpose. A wrong merge tucks a different story behind another one,
while a missed merge only shows two items where one would do. So an item joins a group only when
it matches the group's *leader* (not a chain of neighbours) on at least two words that together
carry real weight and make up half of the shorter headline. Headline words cannot tell an event
("Fable 5.2") from a compound noun ("data center"), so some false merges remain; they are never
hidden, because every folded item stays linked under its story.

Why these rules, in short: word weights are *flattened* (anything rare enough counts fully),
since plain IDF would make a big story penalise itself: the more outlets say "Fable", the less
"Fable" would be worth. Articles lead groups and community threads only join, because a Reddit
title of two words would otherwise pull a group away from the articles it is about. Matching
against every member's words ("union") was tried and chains template headlines ("X now
available on Y").
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING

from ai_radar.models import Item
from ai_radar.scoring import rank

if TYPE_CHECKING:
    from ai_radar.config import DigestSettings

# Only these kinds describe events. A paper, a repo or a model is an artifact with its own URL.
STORY_KINDS = frozenset({"news", "blog", "discussion", "video"})
# These lead groups and head stories: the thread about an article is not the story.
EDITORIAL_KINDS = frozenset({"news", "blog"})

STOPWORDS = frozenset(
    [
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "also",
        "am",
        "amid",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "like",
        "may",
        "me",
        "might",
        "more",
        "most",
        "must",
        "my",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "us",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "within",
        "without",
        "would",
        "you",
        "your",
        "says",
        "say",
        "said",
        "reportedly",
        "report",
        "reports",
        "new",
        "launches",
        "launch",
        "launched",
        "releases",
        "release",
        "released",
        "announces",
        "announced",
        "announce",
        "unveils",
        "unveiled",
        "introduces",
        "introducing",
        "introduced",
        "adds",
        "added",
        "gets",
        "get",
        "got",
        "makes",
        "make",
        "made",
        "takes",
        "take",
        "took",
        "shows",
        "show",
        "showed",
        "everything",
        "know",
        "need",
        "want",
        "first",
        "one",
        "two",
        "three",
        "week",
        "weekly",
        "day",
        "today",
        "tomorrow",
        "yesterday",
        "year",
        "years",
        "time",
        "via",
        "per",
        "versus",
        "available",
        "update",
        "updates",
        "latest",
        "guide",
        "using",
        "use",
        "uses",
        "version",
        "support",
        "supports",
        "ai",
        "artificial",
        "intelligence",
    ]
)
_TOKEN = re.compile(r"\d+\.\d+(?:\.\d+)*|[a-z][a-z0-9]*")


@dataclass(frozen=True, slots=True)
class StoryParams:
    """Knobs of the grouping. The defaults were calibrated on real days of crawled headlines."""

    min_shared: float = 1.4  # the shared words must weigh at least this much (0..1 per word)
    min_overlap: float = 0.5  # ...and make up this fraction of the shorter headline's weight
    min_words: int = 2  # a single shared word, however rare, is never enough
    # Words found in at most this many headlines count as fully distinctive.
    rare_min: int = 8
    rare_fraction: float = 0.04
    coverage_bonus: float = 0.8  # score added per additional outlet covering the story
    max_bonus_outlets: int = 5


DEFAULTS = StoryParams()


def params_for(settings: DigestSettings) -> StoryParams:
    """The grouping parameters a digest configuration asks for."""
    return replace(DEFAULTS, min_overlap=settings.story_overlap)


def title_tokens(title: str) -> frozenset[str]:
    """The words of a headline that can identify an event: names, products, version numbers."""
    text = title.lower().replace("’", "'")
    text = re.sub(r"'s\b", "", text)
    tokens: set[str] = set()
    for token in _TOKEN.findall(text):
        if token in STOPWORDS:
            continue
        if token[0].isdigit():  # a version number such as 5.2
            tokens.add(token)
        elif len(token) >= 3:
            if len(token) > 4 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
                token = token[:-1]  # models -> model
            tokens.add(token)
    return frozenset(tokens)


@dataclass(slots=True)
class Story:
    """One event: the item to show, plus the other coverage of it."""

    head: Item
    score: float  # the best member's score plus a bonus for every extra outlet
    others: list[Item] = field(default_factory=list)  # best first

    @property
    def items(self) -> list[Item]:
        return [self.head, *self.others]

    @property
    def outlets(self) -> int:
        return len({item.source_id for item in self.items})

    def coverage(self) -> list[tuple[Item, int]]:
        """The other coverage by outlet, best first: (that outlet's best item, how many it has)."""
        best: dict[str, Item] = {}
        counts: Counter[str] = Counter()
        for item in self.others:
            best.setdefault(item.source_name, item)
            counts[item.source_name] += 1
        return [(item, counts[name]) for name, item in best.items()]


Entry = tuple[float, Item]


def _group(entries: list[Entry], params: StoryParams) -> list[list[Entry]]:
    """Leader clustering over headline words. ``entries`` must be in processing order."""
    # Sorted words everywhere: the result must not depend on Python's per-run hash seed.
    words_of = [sorted(title_tokens(item.title)) for _, item in entries]
    size = len(entries)
    frequency = Counter(t for words in words_of for t in words)
    floor = min(max(params.rare_min, params.rare_fraction * size), size / 4) or 1.0
    scale = math.log((size + 1) / floor) or 1.0
    weight = {
        t: min(1.0, math.log((size + 1) / max(count, floor)) / scale)
        for t, count in frequency.items()
    }
    # Words in a large share of all headlines say nothing about any one event: never look them up.
    common = {t for t, count in frequency.items() if count > max(40, 0.2 * size)}
    mass = [sum(weight[t] for t in words) for words in words_of]

    groups: list[list[int]] = []  # entry positions per group; the first is the leader
    index: dict[str, list[int]] = defaultdict(list)  # word -> groups whose leader has it

    for position, words in enumerate(words_of):
        shared: dict[int, float] = defaultdict(float)
        counts: Counter[int] = Counter()
        for t in words:
            if t in common:
                continue
            for group in index.get(t, ()):
                shared[group] += weight[t]
                counts[group] += 1
        best, best_key = -1, (0.0, 0)
        for group, total in shared.items():
            if counts[group] < params.min_words or total < params.min_shared:
                continue
            smaller = min(mass[position], mass[groups[group][0]])
            if smaller <= 0 or total / smaller < params.min_overlap:
                continue
            key = (total, -group)  # the heavier match wins; ties go to the earlier group
            if best < 0 or key > best_key:
                best, best_key = group, key
        if best >= 0:
            groups[best].append(position)
            continue
        groups.append([position])
        for t in words:
            if t not in common:
                index[t].append(len(groups) - 1)
    return [[entries[i] for i in group] for group in groups]


def _story(group: list[Entry], params: StoryParams) -> Story:
    # The leader heads the story. Articles are processed before threads, so a group that has an
    # article in it is led by one.
    _, head = group[0]
    others = sorted((e for e in group if e[1] is not head), key=lambda e: -e[0])
    outlets = len({item.source_id for _, item in group}) - 1
    bonus = params.coverage_bonus * min(outlets, params.max_bonus_outlets)
    return Story(
        head=head,
        score=round(max(score for score, _ in group) + bonus, 3),
        others=[item for _, item in others],
    )


def build_stories(
    items: list[Item],
    now: datetime,
    *,
    group: bool = True,
    params: StoryParams = DEFAULTS,
) -> list[Story]:
    """Rank items and fold coverage of one event into a single story. Best story first."""
    ranked = rank(items, now)
    # Articles lead groups; a Reddit or HN thread may join one but never lead it.
    eventful = [e for e in ranked if group and e[1].kind in EDITORIAL_KINDS]
    eventful += [e for e in ranked if group and e[1].kind in STORY_KINDS - EDITORIAL_KINDS]
    groups = _group(eventful, params) if eventful else []
    groups += [[e] for e in ranked if not (group and e[1].kind in STORY_KINDS)]
    stories = [_story(g, params) for g in groups]
    stories.sort(key=lambda s: (-s.score, s.head.title.lower()))
    return stories
