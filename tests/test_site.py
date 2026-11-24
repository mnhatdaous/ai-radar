from datetime import date
from pathlib import Path

import pytest

from ai_radar import cli
from ai_radar.site import FEED_NAME, build_site, read_days, summarize


def digest(
    day: str, *, items: int = 557, stories: int = 512, top: str = "Top headline", extra: str = ""
) -> str:
    return f"""# AI Radar · {day}

> **{items} new items** grouped into **{stories} stories** · news 3 · 2 of 2 sources ok

## Top stories

1. **[{top}](https://e.test/top)** — Lab · 2h ago · `llm`
   - A summary.
   - covered by [Acme](https://acme.test/a "Acme take"), [Daily](https://daily.test/d "Daily take") ×2

## News (2)

- **[Plain story](https://e.test/n)** — Outlet · 3h ago
  - Excerpt.
{extra}
_Generated {day} 06:00 UTC by ai-radar._
"""


def write_digests(folder: Path, *days: str, **kw) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for day in days:
        (folder / f"{day}.md").write_text(digest(day, top=f"Top of {day}", **kw), encoding="utf-8")
    return folder


# ---- reading digests ---------------------------------------------------------------------


def test_summarize_reads_counts_and_the_top_headline() -> None:
    assert summarize(digest("2026-11-10")) == (557, 512, "Top headline")
    assert summarize("# x\n\n> **3 new items** · news 2\n") == (3, 3, None)  # nothing was grouped
    assert summarize("# x\n\n_Nothing new today._\n") == (None, None, None)
    escaped = "# x\n\n1. **[Use \\[brackets\\] here](https://e.test)** — a\n"
    assert summarize(escaped)[2] == "Use [brackets] here"


def test_read_days_ignores_everything_but_dated_digests(tmp_path: Path) -> None:
    write_digests(tmp_path, "2026-11-11", "2026-11-09")
    (tmp_path / "latest.md").write_text("a copy of the newest digest", encoding="utf-8")
    (tmp_path / "notes.md").write_text("not a digest", encoding="utf-8")
    (tmp_path / "2026-13-45.md").write_text("not a date", encoding="utf-8")
    assert [d.day for d in read_days(tmp_path)] == [date(2026, 11, 9), date(2026, 11, 11)]
    assert read_days(tmp_path / "missing") == []


# ---- the site ----------------------------------------------------------------------------


def test_the_site_has_a_page_per_day_an_index_an_archive_and_the_feed(tmp_path: Path) -> None:
    digests = write_digests(tmp_path / "digests", "2026-11-09", "2026-11-10", "2026-11-11")
    feed = tmp_path / "feed.atom"
    feed.write_text("<feed/>", encoding="utf-8")
    out = tmp_path / "site"

    written = build_site(digests, out, feed=feed, repo="me/ai-radar")

    assert sorted(p.name for p in written) == sorted(
        [
            ".nojekyll",
            FEED_NAME,
            "2026-11-09.html",
            "2026-11-10.html",
            "2026-11-11.html",
            "archive.html",
            "index.html",
        ]
    )
    assert (out / FEED_NAME).read_text() == "<feed/>"

    index = (out / "index.html").read_text()
    assert "Top of 2026-11-11" in index and "Top of 2026-11-10" not in index  # the newest digest
    assert 'rel="alternate" type="application/atom+xml"' in index  # feed autodiscovery
    assert 'href="https://github.com/me/ai-radar"' in index

    first = (out / "2026-11-09.html").read_text()
    middle = (out / "2026-11-10.html").read_text()
    last = (out / "2026-11-11.html").read_text()
    assert "←" not in first and "→" in first  # no earlier day
    assert 'href="2026-11-09.html"' in middle and 'href="2026-11-11.html"' in middle
    assert "←" in last and "→" not in last  # no later day

    archive = (out / "archive.html").read_text()
    assert archive.index("2026-11-11") < archive.index("2026-11-10") < archive.index("2026-11-09")
    assert '<td class="num">557</td><td class="num">512</td><td>Top of 2026-11-11</td>' in archive


def test_a_day_page_has_anchors_a_filter_and_link_tooltips(tmp_path: Path) -> None:
    out = tmp_path / "site"
    build_site(write_digests(tmp_path / "d", "2026-11-10"), out)
    page = (out / "2026-11-10.html").read_text()

    assert "<title>AI Radar · 2026-11-10</title>" in page
    assert (
        '<h2 id="top-stories">Top stories</h2>' in page and '<h2 id="news-2">News (2)</h2>' in page
    )
    assert (
        '<a href="#top-stories">Top stories</a>' in page
        and '<a href="#news-2">News (2)</a>' in page
    )
    assert (
        'href="https://acme.test/a" title="Acme take"' in page
    )  # the folded headline is a tooltip
    assert 'id="q"' in page and '<main id="digest">' in page
    assert '<link rel="alternate"' not in page  # no feed was given


def test_untrusted_headlines_cannot_inject_html_or_script(tmp_path: Path) -> None:
    evil = (
        "- **[<img src=x onerror=alert(1)> <script>alert(2)</script>](https://e.test)** — x\n"
        "- **[click](javascript:alert(3))** — x\n"
        '- [a](https://e.test "bad\\" onmouseover=\\"alert(4)")\n'
    )
    out = tmp_path / "site"
    build_site(write_digests(tmp_path / "d", "2026-11-10", extra=evil), out)
    page = (out / "2026-11-10.html").read_text()

    assert "<img" not in page and "<script>alert(2)</script>" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page  # shown as text
    assert 'href="javascript:' not in page
    assert ' onmouseover="alert(4)"' not in page  # the quote stayed inside the title attribute
    assert page.count("<script>") == 1  # only the page's own filter script


@pytest.mark.parametrize("repo", ['me/x" onmouseover="y', "no-slash", "a/b/c", ""])
def test_a_malformed_repo_name_is_ignored(tmp_path: Path, repo: str) -> None:
    out = tmp_path / "site"
    build_site(write_digests(tmp_path / "d", "2026-11-10"), out, repo=repo)
    page = (out / "index.html").read_text()
    assert "github.com/" not in page and "onmouseover" not in page


def test_an_empty_digest_folder_still_builds_a_site(tmp_path: Path) -> None:
    out = tmp_path / "site"
    build_site(tmp_path / "none", out)
    assert "No digests yet" in (out / "index.html").read_text()
    assert "No digests yet" in (out / "archive.html").read_text()
    assert not (out / FEED_NAME).exists()


def test_building_twice_gives_identical_files(tmp_path: Path) -> None:
    digests = write_digests(tmp_path / "d", "2026-11-09", "2026-11-10")
    for name in ("a", "b"):
        build_site(digests, tmp_path / name, repo="me/ai-radar")
    for page in sorted((tmp_path / "a").iterdir()):
        assert page.read_bytes() == (tmp_path / "b" / page.name).read_bytes(), page.name


# ---- command -----------------------------------------------------------------------------


def test_the_site_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_digests(tmp_path / "digests", "2026-11-10")
    (tmp_path / "feeds").mkdir()
    (tmp_path / "feeds" / FEED_NAME).write_text("<feed/>", encoding="utf-8")

    assert cli.main(["--root", str(tmp_path), "site"]) == 0
    assert (tmp_path / "site" / "index.html").exists() and (tmp_path / "site" / FEED_NAME).exists()
    assert "site" in capsys.readouterr().out

    elsewhere = tmp_path / "elsewhere"
    assert (
        cli.main(
            ["--root", str(tmp_path), "site", "--out", str(elsewhere), "--repo", "me/ai-radar"]
        )
        == 0
    )
    assert "https://github.com/me/ai-radar" in (elsewhere / "index.html").read_text()


def test_the_repo_defaults_to_the_github_actions_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_digests(tmp_path / "digests", "2026-11-10")
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/from-actions")
    assert cli.main(["--root", str(tmp_path), "site"]) == 0
    assert "https://github.com/me/from-actions" in (tmp_path / "site" / "index.html").read_text()
