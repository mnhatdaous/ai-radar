from datetime import timedelta

import pytest

from ai_radar.models import Mention
from ai_radar.scoring import score_item
from helpers import NOW, make_item


def test_newer_items_score_higher() -> None:
    fresh = make_item(published_at=NOW - timedelta(hours=1))
    stale = make_item(published_at=NOW - timedelta(days=10))
    assert score_item(fresh, NOW) > score_item(stale, NOW)


def test_trusted_sources_score_higher() -> None:
    assert score_item(make_item(weight=1.0), NOW) > score_item(make_item(weight=0.3), NOW)


def test_popularity_counts_but_is_log_scaled() -> None:
    quiet = score_item(make_item(engagement=10), NOW)
    loud = score_item(make_item(engagement=1000), NOW)
    assert 0 < loud - quiet < 3.0  # 100x the votes is worth a couple of points, not 100x


def test_corroboration_adds_up_to_a_cap() -> None:
    def mentions(n: int) -> list[Mention]:
        return [Mention(f"s{i}", f"S{i}", f"https://e.test/{i}") for i in range(n)]

    base = score_item(make_item(), NOW)
    assert score_item(make_item(mentions=mentions(2)), NOW) == pytest.approx(base + 1.6, abs=0.002)
    assert score_item(make_item(mentions=mentions(9)), NOW) == pytest.approx(base + 3.2, abs=0.002)


def test_undated_items_are_scored_from_when_they_were_first_seen() -> None:
    undated = make_item(published_at=None, first_seen_at=NOW)
    old = make_item(published_at=NOW - timedelta(days=5))
    assert score_item(undated, NOW) > score_item(old, NOW)


def test_future_dates_do_not_inflate_scores() -> None:
    future = make_item(published_at=NOW + timedelta(days=3))
    now_ = make_item(published_at=NOW)
    assert score_item(future, NOW) == score_item(now_, NOW)
