"""Plain data containers shared across the crawler."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

KINDS: tuple[str, ...] = (
    "news",
    "blog",
    "paper",
    "repo",
    "tool",
    "model",
    "dataset",
    "release",
    "discussion",
    "video",
)

_ISO = "%Y-%m-%dT%H:%M:%SZ"


def to_iso(dt: datetime | None) -> str | None:
    """UTC ISO-8601 with a trailing Z. Strings in this format sort like the times they encode."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime(_ISO)


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, _ISO).replace(tzinfo=UTC)


@dataclass(slots=True)
class RawItem:
    """What a source returns, before normalisation, filtering and dedupe."""

    url: str
    title: str
    summary: str = ""
    author: str | None = None
    published_at: datetime | None = None
    kind: str | None = None  # falls back to the source's configured kind
    discussion_url: str | None = None  # HN / Reddit / Lobsters thread, HF paper page, ...
    metrics: dict[str, float] = field(default_factory=dict)  # namespaced: hn_points, stars, ...
    engagement: float = 0.0  # popularity on the source's own scale, >= 0
    publisher: str | None = None  # display name override, e.g. the outlet behind an aggregator
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Mention:
    """Another source that pointed at the same canonical URL."""

    source_id: str
    source_name: str
    url: str
    title: str = ""
    metrics: dict[str, float] = field(default_factory=dict)
    seen_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "url": self.url,
            "title": self.title,
            "metrics": self.metrics,
            "seen_at": to_iso(self.seen_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Mention:
        return cls(
            source_id=data["source_id"],
            source_name=data.get("source_name", data["source_id"]),
            url=data["url"],
            title=data.get("title", ""),
            metrics=dict(data.get("metrics") or {}),
            seen_at=from_iso(data.get("seen_at")),
        )


@dataclass(slots=True)
class Item:
    """A normalised, deduplicated piece of AI content."""

    id: str
    url: str  # link shown to people: tracking params stripped, otherwise untouched
    canonical_url: str  # identity used for dedupe
    title: str
    summary: str
    author: str | None
    published_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime
    source_id: str
    source_name: str
    kind: str
    weight: float  # trust weight of the (best) source that reported it
    topics: list[str]
    tags: list[str]
    metrics: dict[str, float]
    engagement: float
    discussion_url: str | None
    domain: str
    mentions: list[Mention] = field(default_factory=list)


def item_to_dict(item: Item) -> dict[str, Any]:
    return {
        "id": item.id,
        "url": item.url,
        "canonical_url": item.canonical_url,
        "title": item.title,
        "summary": item.summary,
        "author": item.author,
        "published_at": to_iso(item.published_at),
        "first_seen_at": to_iso(item.first_seen_at),
        "last_seen_at": to_iso(item.last_seen_at),
        "source_id": item.source_id,
        "source_name": item.source_name,
        "kind": item.kind,
        "weight": item.weight,
        "topics": item.topics,
        "tags": item.tags,
        "metrics": item.metrics,
        "engagement": item.engagement,
        "discussion_url": item.discussion_url,
        "domain": item.domain,
        "mentions": [m.to_dict() for m in item.mentions],
    }


def item_from_dict(data: dict[str, Any]) -> Item:
    first_seen = from_iso(data["first_seen_at"])
    assert first_seen is not None
    return Item(
        id=data["id"],
        url=data["url"],
        canonical_url=data["canonical_url"],
        title=data["title"],
        summary=data.get("summary", ""),
        author=data.get("author"),
        published_at=from_iso(data.get("published_at")),
        first_seen_at=first_seen,
        last_seen_at=from_iso(data.get("last_seen_at")) or first_seen,
        source_id=data["source_id"],
        source_name=data.get("source_name", data["source_id"]),
        kind=data["kind"],
        weight=float(data.get("weight", 0.5)),
        topics=list(data.get("topics") or []),
        tags=list(data.get("tags") or []),
        metrics=dict(data.get("metrics") or {}),
        engagement=float(data.get("engagement", 0.0)),
        discussion_url=data.get("discussion_url"),
        domain=data.get("domain", ""),
        mentions=[Mention.from_dict(m) for m in data.get("mentions") or []],
    )
