"""Ranking. Time-dependent, so it is computed when a digest is rendered, never stored."""

from __future__ import annotations

import math
from datetime import datetime

from ai_radar.models import Item

RECENCY_HALF_LIFE_HOURS = 48.0


def score_item(item: Item, now: datetime) -> float:
    """Higher is more interesting.

    - source trust (``weight`` 0..1) is the base, up to 4 points
    - recency adds up to 3 points and decays with a ~2 day scale
    - popularity is log-scaled so 1000 HN points is not 100x a 10-point post
    - every extra source that mentioned the same URL is corroboration, capped at 4
    """
    reference = item.published_at or item.first_seen_at
    age_hours = max((now - reference).total_seconds() / 3600.0, 0.0)
    recency = 3.0 * math.exp(-age_hours / RECENCY_HALF_LIFE_HOURS)
    base = 4.0 * item.weight
    popularity = 1.2 * math.log10(1.0 + max(item.engagement, 0.0))
    corroboration = 0.8 * min(len(item.mentions), 4)
    return round(base + recency + popularity + corroboration, 3)


def rank(items: list[Item], now: datetime) -> list[tuple[float, Item]]:
    """Items with their scores, best first. Ties are broken by title so output is stable."""
    scored = [(score_item(item, now), item) for item in items]
    scored.sort(key=lambda pair: (-pair[0], pair[1].title.lower()))
    return scored
