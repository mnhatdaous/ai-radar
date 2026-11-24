"""Durable outputs: the per-day JSONL archive and an Atom feed you can subscribe to."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from ai_radar.config import DigestSettings
from ai_radar.models import Item, item_to_dict, to_iso
from ai_radar.stories import Story, build_stories, params_for

_ATOM = "http://www.w3.org/2005/Atom"
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def write_text(path: Path, text: str) -> None:
    _atomic_write(path, text)


def write_day_archive(items: Iterable[Item], path: Path) -> int:
    """Write (merging with whatever is already there) one day's items as JSON Lines.

    Merging keeps records another machine archived for the same day; the new version of a record
    wins because it carries the latest mentions and metrics.
    """
    records: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                records[record["id"]] = record
            except (ValueError, KeyError):
                continue
    for item in items:
        records[item.id] = item_to_dict(item)
    ordered = sorted(records.values(), key=lambda r: (r["first_seen_at"], r["id"]))
    body = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in ordered)
    _atomic_write(path, body)
    return len(ordered)


def feed_selection(
    items: list[Item],
    now: datetime,
    *,
    settings: DigestSettings | None = None,
    limit: int = 100,
) -> list[Story]:
    """The most interesting recent stories, newest first. Coverage of one event is one entry."""
    settings = settings or DigestSettings()
    stories = build_stories(items, now, group=settings.group_stories, params=params_for(settings))
    return sorted(
        stories[:limit], key=lambda s: s.head.published_at or s.head.first_seen_at, reverse=True
    )


def _safe(text: str) -> str:
    return _XML_ILLEGAL.sub("", text)


def write_atom(stories: list[Story], path: Path, *, now: datetime, title: str = "AI Radar") -> None:
    ET.register_namespace("", _ATOM)

    def tag(name: str) -> str:
        return f"{{{_ATOM}}}{name}"

    feed = ET.Element(tag("feed"))
    ET.SubElement(feed, tag("title")).text = title
    ET.SubElement(feed, tag("id")).text = "urn:ai-radar:feed"
    ET.SubElement(feed, tag("updated")).text = to_iso(now)
    ET.SubElement(ET.SubElement(feed, tag("author")), tag("name")).text = "ai-radar"
    for story in stories:
        item = story.head
        entry = ET.SubElement(feed, tag("entry"))
        ET.SubElement(entry, tag("title")).text = _safe(item.title)
        ET.SubElement(entry, tag("id")).text = _safe(item.canonical_url)
        ET.SubElement(entry, tag("link"), href=_safe(item.url))
        ET.SubElement(entry, tag("updated")).text = to_iso(item.published_at or item.first_seen_at)
        ET.SubElement(entry, tag("summary"), type="text").text = _safe(item.summary or item.title)
        ET.SubElement(entry, tag("category"), term=item.kind)
        for topic in item.topics:
            ET.SubElement(entry, tag("category"), term=topic)
        for other in story.others[:10]:  # the rest of the coverage of the same event
            ET.SubElement(
                entry, tag("link"), rel="related", href=_safe(other.url), title=_safe(other.title)
            )
    ET.indent(feed)
    body = ET.tostring(feed, encoding="unicode", xml_declaration=False)
    _atomic_write(path, '<?xml version="1.0" encoding="utf-8"?>\n' + body + "\n")
