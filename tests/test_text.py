import time
from datetime import UTC, datetime

import pytest

from ai_radar.text import clean_html, parse_datetime, truncate


def test_clean_html_strips_markup_and_entities() -> None:
    assert clean_html("<p>Hello &amp; <b>world</b></p><script>x()</script>") == "Hello & world"
    assert clean_html(None) == ""
    assert clean_html("  plain   text \n here ") == "plain text here"


def test_clean_html_removes_feed_boilerplate() -> None:
    wordpress = "Great news today. The post Big thing appeared first on Some Blog."
    assert clean_html(wordpress) == "Great news today."
    assert clean_html("Intro text […] Read more »") == "Intro text"
    assert clean_html("<p>Intro</p> <a href='/x'>Continue reading</a>") == "Intro"


def test_truncate() -> None:
    assert truncate("short", 10) == "short"
    assert truncate("alpha beta gamma delta", 12) == "alpha beta…"
    assert truncate("abcdefghij", 5) == "abcde…"
    assert truncate("anything", 0) == "anything"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-10T09:30:00Z", datetime(2026, 11, 10, 9, 30, tzinfo=UTC)),
        ("2026-11-09T12:00:00+07:00", datetime(2026, 11, 9, 5, 0, tzinfo=UTC)),
        ("2026-11-09T12:00:00", datetime(2026, 11, 9, 12, 0, tzinfo=UTC)),  # naive means UTC
        ("Sat, 07 Nov 2026 13:34:01 -0600", datetime(2026, 11, 7, 19, 34, 1, tzinfo=UTC)),
        ("20261110T093000Z", datetime(2026, 11, 10, 9, 30, tzinfo=UTC)),  # GDELT
        ("2026-11-09", datetime(2026, 11, 9, tzinfo=UTC)),
        (1794284545, datetime.fromtimestamp(1794284545, UTC)),
        (1794284545000, datetime.fromtimestamp(1794284545, UTC)),  # milliseconds
        (
            time.struct_time((2026, 11, 9, 12, 0, 0, 0, 0, 0)),
            datetime(2026, 11, 9, 12, 0, tzinfo=UTC),
        ),
    ],
)
def test_parse_datetime(value: object, expected: datetime) -> None:
    assert parse_datetime(value) == expected


@pytest.mark.parametrize("value", [None, "", "   ", "garbage", "next tuesday"])
def test_parse_datetime_gives_none_for_junk(value: object) -> None:
    assert parse_datetime(value) is None
