"""The daily Markdown digest: what is new, grouped into stories, ranked, with links out."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from typing import Any

from ai_radar.config import DigestSettings
from ai_radar.models import Item
from ai_radar.stories import Story, build_stories, params_for
from ai_radar.text import truncate
from ai_radar.urls import domain_of

SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("News", ("news",)),
    ("Labs, companies and newsletters", ("blog",)),
    ("Research papers", ("paper",)),
    ("Tools and launches", ("tool",)),
    ("Open-source repos", ("repo",)),
    ("Models and datasets", ("model", "dataset")),
    ("Releases", ("release",)),
    ("Discussions", ("discussion",)),
    ("Videos", ("video",)),
)
_DISCUSSION_LABELS = {
    "news.ycombinator.com": "HN",
    "lobste.rs": "Lobsters",
    "reddit.com": "Reddit",
    "huggingface.co": "HF",
}
COVERAGE_SHOWN = 6  # outlets listed under a story before "+N more"


def md_text(value: str) -> str:
    """Escape text placed on a Markdown line or inside link text (feeds are untrusted input)."""
    value = " ".join(value.split())
    for char in ("\\", "[", "]", "<", ">", "`"):
        value = value.replace(char, "\\" + char)
    return value


def md_url(url: str) -> str:
    for char, code in ((" ", "%20"), ("(", "%28"), (")", "%29"), ("<", "%3C"), (">", "%3E")):
        url = url.replace(char, code)
    return url


def md_link_title(value: str) -> str:
    """Text for the "tooltip" of a Markdown link: [text](url "tooltip")."""
    value = truncate(" ".join(value.split()), 120)
    return value.replace("\\", "\\\\").replace('"', '\\"')


def ago(now: datetime, then: datetime) -> str:
    seconds = max((now - then).total_seconds(), 0.0)
    if seconds < 3600:
        return f"{max(int(seconds // 60), 1)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _short_number(value: float) -> str:
    return f"{value / 1000:.1f}k" if value >= 1000 else str(int(value))


def metric_bits(item: Item) -> list[str]:
    m = item.metrics
    bits: list[str] = []
    if m.get("hn_points"):
        comments = f" · {int(m['hn_comments'])} comments" if m.get("hn_comments") else ""
        bits.append(f"HN {int(m['hn_points'])} pts{comments}")
    if m.get("stars_today"):
        bits.append(f"★ {_short_number(m.get('stars', 0))} (+{int(m['stars_today'])} today)")
    elif m.get("stars"):
        bits.append(f"★ {_short_number(m['stars'])}")
    if m.get("hf_upvotes"):
        bits.append(f"▲ {int(m['hf_upvotes'])} on HF")
    elif m.get("hf_likes"):
        bits.append(f"♥ {_short_number(m['hf_likes'])} on HF")
    if m.get("reddit_rank"):
        bits.append(f"Reddit #{int(m['reddit_rank'])} today")
    return bits


def discussion_links(item: Item) -> tuple[str | None, list[str]]:
    """(the main discussion thread, other places that mentioned the same link)."""
    thread: str | None = None
    seen: set[str] = set()
    if item.discussion_url:
        label = _DISCUSSION_LABELS.get(domain_of(item.discussion_url), "thread")
        thread = f"[{label}]({md_url(item.discussion_url)})"
        seen.add(item.discussion_url)
    also: list[str] = []
    for mention in item.mentions:
        if mention.url in seen or mention.url == item.url:
            continue
        seen.add(mention.url)
        also.append(f"[{md_text(mention.source_name)}]({md_url(mention.url)})")
    return thread, also[:3]


def coverage_line(story: Story, limit: int = COVERAGE_SHOWN) -> str | None:
    """'covered by The Verge, TechCrunch ×5 +3 more': the rest of a story, one link per outlet.

    Each link's tooltip is the headline, so what was folded in stays inspectable.
    """
    coverage = story.coverage()
    if not coverage:
        return None
    parts = []
    for item, count in coverage[:limit]:
        # For a community thread, the thread itself is the thing to point at.
        target = (
            item.discussion_url if item.kind == "discussion" and item.discussion_url else item.url
        )
        part = f'[{md_text(item.source_name)}]({md_url(target)} "{md_link_title(item.title)}")'
        parts.append(part + (f" ×{count}" if count > 1 else ""))
    hidden = sum(count for _, count in coverage[limit:])
    return "covered by " + ", ".join(parts) + (f" +{hidden} more" if hidden else "")


def render_entry(
    item: Item,
    now: datetime,
    settings: DigestSettings,
    bullet: str = "-",
    story: Story | None = None,
) -> list[str]:
    indent = " " * (len(bullet) + 1)  # nested bullets must line up with the item's text
    head = [md_text(item.source_name), ago(now, item.published_at) if item.published_at else "new"]
    if item.topics:
        head.append(" ".join(f"`{t}`" for t in item.topics))
    lines = [f"{bullet} **[{md_text(item.title)}]({md_url(item.url)})** — {' · '.join(head)}"]
    if item.summary:
        lines.append(f"{indent}- {md_text(truncate(item.summary, settings.summary_chars))}")
    extras = metric_bits(item)
    thread, also = discussion_links(item)
    if thread:
        extras.append(f"discussion: {thread}")
    if also:
        extras.append("also: " + ", ".join(also))
    if extras:
        lines.append(f"{indent}- " + " · ".join(extras))
    covered = coverage_line(story) if story is not None else None
    if covered:
        lines.append(f"{indent}- {covered}")
    return lines


# Which kinds compete for the same front-page slots (quotas live in settings.digest.top_mix).
TOP_GROUPS: dict[str, tuple[str, ...]] = {
    "editorial": ("news", "blog"),
    "community": ("discussion", "video"),
    "builders": ("paper", "repo", "tool", "model", "dataset", "release"),
}
_GROUP_OF = {kind: group for group, kinds in TOP_GROUPS.items() for kind in kinds}


def pick_top(stories: list[Story], settings: DigestSettings) -> list[Story]:
    """A front page with a mix, so trending repos or HN threads cannot crowd out the news.

    ``stories`` must be best first. Each group gets its quota of the best stories; slots a group
    cannot fill go to the best of the rest. No source may head more than
    ``max_per_source_in_top`` of the stories.
    """
    picked: list[Story] = []
    chosen: set[str] = set()
    per_source: Counter[str] = Counter()
    taken: Counter[str] = Counter()

    def take(story: Story) -> None:
        picked.append(story)
        chosen.add(story.head.id)
        per_source[story.head.source_id] += 1
        taken[_GROUP_OF.get(story.head.kind, "builders")] += 1

    cap = settings.max_per_source_in_top
    for story in stories:
        group = _GROUP_OF.get(story.head.kind, "builders")
        if (
            len(picked) < settings.top_n
            and taken[group] < settings.top_mix.get(group, 0)
            and per_source[story.head.source_id] < cap
        ):
            take(story)
    for story in stories:
        if len(picked) >= settings.top_n:
            break
        if story.head.id not in chosen and per_source[story.head.source_id] < cap:
            take(story)
    picked.sort(key=lambda s: -s.score)
    return picked


def render_digest(
    items: list[Item],
    *,
    day: date,
    now: datetime,
    settings: DigestSettings,
    health: list[dict[str, Any]] | None = None,
) -> str:
    health = health or []
    stories = build_stories(items, now, group=settings.group_stories, params=params_for(settings))
    top = pick_top(stories, settings)
    top_ids = {story.head.id for story in top}
    kinds = Counter(item.kind for item in items)
    failed = [h for h in health if not h["ok"]]
    partial = [h for h in health if h["ok"] and h["warnings"]]

    summary = f"**{len(items)} new items**"
    if len(stories) < len(items):
        summary += f" grouped into **{len(stories)} stories**"
    if kinds:
        summary += " · " + " · ".join(f"{kind} {n}" for kind, n in kinds.most_common())
    if health:
        summary += f" · {len(health) - len(failed)} of {len(health)} sources ok"

    lines = [f"# AI Radar · {day.isoformat()}", "", f"> {summary}", ""]
    if not items:
        lines += ["_Nothing new today._", ""]

    if top:
        lines += ["## Top stories", ""]
        for number, story in enumerate(top, start=1):
            lines += render_entry(story.head, now, settings, bullet=f"{number}.", story=story)
        lines.append("")

    for title, section_kinds in SECTIONS:
        section = [s for s in stories if s.head.kind in section_kinds]
        pool = [s for s in section if s.head.id not in top_ids]
        if not pool:
            continue
        lines += [f"## {title} ({len(section)})", ""]
        for story in pool[: settings.max_per_section]:
            lines += render_entry(story.head, now, settings, story=story)
        hidden = len(pool) - settings.max_per_section
        if hidden > 0:
            lines.append(f"- _…and {hidden} more stories in the day's archive file._")
        lines.append("")

    if failed or partial:
        lines += ["---", "", "### Source health", ""]
        for h in failed:
            lines.append(f"- `{h['source_id']}` failed: {md_text(h['error'] or 'unknown error')}")
        for h in partial:
            lines.append(
                f"- `{h['source_id']}` partly failed: {md_text('; '.join(h['warnings'][:2]))}"
            )
        lines.append("")
    lines.append(f"_Generated {now:%Y-%m-%d %H:%M} UTC by ai-radar._")
    return "\n".join(lines) + "\n"
