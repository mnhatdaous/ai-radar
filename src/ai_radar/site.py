"""A static website for the digests: what GitHub Pages serves right after each crawl.

The digests (``digests/YYYY-MM-DD.md``) are the report. This turns them into linked HTML pages,
a list of every day and a copy of the Atom feed. No database and no network are involved, so the
site can be rebuilt from the repo alone.
"""

from __future__ import annotations

import html
import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from markdown_it import MarkdownIt

FEED_NAME = "ai-radar.atom"

# Feed text is untrusted: raw HTML in a digest is escaped, never passed through to the page, and
# markdown-it refuses javascript: and similar link schemes.
_MARKDOWN = MarkdownIt("commonmark", {"html": False})
_DIGEST_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_SUMMARY = re.compile(r"\*\*(\d+) new items\*\*(?: grouped into \*\*(\d+) stories\*\*)?")
_TOP_STORY = re.compile(r"^1\. \*\*\[((?:\\.|[^\]\\])*)\]\(", re.MULTILINE)
_H2 = re.compile(r"<h2>(.*?)</h2>")
_TAG = re.compile(r"<[^>]+>")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

_CSS = """
:root { color-scheme: light dark; --bg: #fff; --fg: #1b1b1f; --muted: #5f6368;
  --line: #e4e4e7; --chip: #f1f3f4; --accent: #0b57d0; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #121212; --fg: #e6e6e6; --muted: #9aa0a6; --line: #2c2c2e; --chip: #26282b;
    --accent: #8ab4f8; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 860px; margin: 0 auto; padding: 0 1rem 4rem; }
header.top { display: flex; flex-wrap: wrap; gap: .4rem 1rem; align-items: baseline;
  justify-content: space-between; padding: 1.2rem 0 .7rem; border-bottom: 1px solid var(--line); }
header.top .brand { font-weight: 700; font-size: 1.1rem; color: var(--fg); }
header.top nav { display: flex; gap: 1rem; font-size: .92rem; }
h1 { font-size: 1.5rem; margin: 1.2rem 0 .4rem; }
h2 { font-size: 1.15rem; margin: 2.2rem 0 .4rem; padding-bottom: .3rem;
  border-bottom: 1px solid var(--line); scroll-margin-top: 1rem; }
h3 { font-size: 1rem; margin: 1.6rem 0 .3rem; }
blockquote { margin: .5rem 0 1rem; padding: .6rem .9rem; background: var(--chip);
  border-radius: 8px; color: var(--muted); font-size: .92rem; }
blockquote p { margin: 0; }
ul, ol { padding-left: 1.4rem; }
li { margin: .7rem 0; }
li > ul { list-style: none; padding-left: 0; margin: .15rem 0 0; color: var(--muted);
  font-size: .9rem; }
li > ul > li { margin: .1rem 0; }
code { font: inherit; font-size: .76rem; background: var(--chip); padding: .05rem .45rem;
  border-radius: 999px; color: var(--muted); }
.jump { display: flex; flex-wrap: wrap; gap: .3rem .9rem; margin: .9rem 0; font-size: .9rem; }
.tools { margin: 1rem 0 .2rem; }
input[type=search] { width: 100%; padding: .55rem .8rem; font: inherit; color: var(--fg);
  background: var(--bg); border: 1px solid var(--line); border-radius: 8px; }
.pager { display: flex; justify-content: space-between; margin: 1.4rem 0; font-size: .92rem; }
footer { margin-top: 3rem; padding-top: 1rem; border-top: 1px solid var(--line);
  color: var(--muted); font-size: .85rem; }
table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
th, td { text-align: left; padding: .5rem .6rem; border-bottom: 1px solid var(--line);
  vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: .85rem; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td:first-child { white-space: nowrap; }
.hidden { display: none; }
"""

# Filters the entries of one page as you type. It only reads the page; nothing from the digests
# is ever placed inside this script.
_JS = """
(function () {
  var box = document.getElementById("q");
  var main = document.getElementById("digest");
  if (!box || !main) return;
  box.addEventListener("input", function () {
    var needle = box.value.trim().toLowerCase();
    var groups = [], current = null;
    Array.prototype.forEach.call(main.children, function (el) {
      if (el.tagName === "H2" || el.tagName === "H3") {
        current = { heading: el, entries: [] };
        groups.push(current);
      } else if (current && (el.tagName === "UL" || el.tagName === "OL")) {
        Array.prototype.forEach.call(el.children, function (li) { current.entries.push(li); });
      }
    });
    groups.forEach(function (group) {
      var shown = 0;
      group.entries.forEach(function (li) {
        var match = !needle || li.textContent.toLowerCase().indexOf(needle) > -1;
        li.classList.toggle("hidden", !match);
        if (match) shown++;
      });
      group.heading.classList.toggle("hidden", !!needle && group.entries.length > 0 && shown === 0);
    });
  });
})();
"""


@dataclass(frozen=True, slots=True)
class Day:
    """One daily digest."""

    day: date
    markdown: str
    items: int | None
    stories: int | None
    top: str | None  # the headline of the first Top story

    @property
    def page(self) -> str:
        return f"{self.day.isoformat()}.html"


def summarize(markdown: str) -> tuple[int | None, int | None, str | None]:
    """(items, stories, top headline) read back from a rendered digest."""
    summary = _SUMMARY.search(markdown)
    items = int(summary[1]) if summary else None
    stories = int(summary[2]) if summary and summary[2] else items
    top = _TOP_STORY.search(markdown)
    return items, stories, re.sub(r"\\(.)", r"\1", top[1]) if top else None


def read_days(digests_dir: Path) -> list[Day]:
    """Every dated digest in the folder, oldest first. ``latest.md`` is a copy, so it is skipped."""
    days = []
    for path in sorted(digests_dir.glob("*.md")) if digests_dir.is_dir() else []:
        match = _DIGEST_FILE.match(path.name)
        if not match:
            continue
        try:
            when = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        text = path.read_text(encoding="utf-8")
        days.append(Day(when, text, *summarize(text)))
    return days


def _slug(heading_html: str) -> str:
    plain = html.unescape(_TAG.sub("", heading_html)).lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-") or "section"


def _pager(previous: Day | None, following: Day | None) -> str:
    left = (
        f'<a href="{previous.page}">← {previous.day.isoformat()}</a>'
        if previous
        else "<span></span>"
    )
    right = (
        f'<a href="{following.page}">{following.day.isoformat()} →</a>'
        if following
        else "<span></span>"
    )
    return f'<div class="pager">{left}{right}</div>'


def _day_body(day: Day, previous: Day | None, following: Day | None) -> str:
    content = _MARKDOWN.render(day.markdown)
    jumps: list[tuple[str, str]] = []

    def add_anchor(match: re.Match[str]) -> str:
        inner = match.group(1)
        ident = _slug(inner)
        jumps.append((ident, html.unescape(_TAG.sub("", inner))))
        return f'<h2 id="{ident}">{inner}</h2>'

    content = _H2.sub(add_anchor, content)
    jump_links = "".join(f'<a href="#{ident}">{html.escape(text)}</a>' for ident, text in jumps)
    pager = _pager(previous, following)
    return (
        f"{pager}"
        '<div class="tools"><input id="q" type="search" placeholder="Filter this page…" '
        'aria-label="Filter this page" autocomplete="off"></div>'
        f'<nav class="jump">{jump_links}</nav>'
        f'<main id="digest">{content}</main>'
        f"{pager}"
    )


def _digests_body(days: list[Day]) -> str:
    if not days:
        return "<h1>Digests</h1><p>No digests yet.</p>"
    rows = []
    for d in reversed(days):  # newest first
        items = "" if d.items is None else str(d.items)
        stories = "" if d.stories is None else str(d.stories)
        top = html.escape(d.top) if d.top else ""
        rows.append(
            f'<tr><td><a href="{d.page}">{d.day.isoformat()}</a></td>'
            f'<td class="num">{items}</td><td class="num">{stories}</td><td>{top}</td></tr>'
        )
    return (
        "<h1>Digests</h1>"
        '<table><thead><tr><th>Day</th><th class="num">Items</th><th class="num">Stories</th>'
        "<th>Top story</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _page(title: str, body: str, *, has_feed: bool, repo: str | None) -> str:
    links = ['<a href="digests.html">Digests</a>']
    feed_link = ""
    if has_feed:
        links.append(f'<a href="{FEED_NAME}">Feed</a>')
        feed_link = f'<link rel="alternate" type="application/atom+xml" title="AI Radar" href="{FEED_NAME}">'
    source = ""
    if repo:
        url = f"https://github.com/{repo}"
        links.append(f'<a href="{url}">GitHub</a>')
        source = f' Source: <a href="{url}">{html.escape(repo)}</a>.'
    nav = " ".join(links)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="A daily digest of AI news, research, tools, models and repos.">
{feed_link}
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
<header class="top"><a class="brand" href="index.html">AI Radar</a><nav>{nav}</nav></header>
{body}
<footer>Built from the daily digests. Headlines and short excerpts come from the sources' own feeds, and every item links back to its source.{source}</footer>
</div>
<script>{_JS}</script>
</body>
</html>
"""


def build_site(
    digests_dir: Path,
    out_dir: Path,
    *,
    feed: Path | None = None,
    repo: str | None = None,
) -> list[Path]:
    """Write the site into ``out_dir`` and return the files written.

    ``index.html`` is the newest digest, ``YYYY-MM-DD.html`` one page per day, ``digests.html``
    lists them all and ``ai-radar.atom`` is a copy of the feed. Nothing in ``out_dir`` is deleted,
    so a stale page can linger if you remove a digest; build into a fresh folder to avoid that.
    """
    days = read_days(digests_dir)
    has_feed = feed is not None and feed.is_file()
    repo = repo if repo and _REPO.fullmatch(repo) else None
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def write(name: str, text: str) -> None:
        path = out_dir / name
        path.write_text(text, encoding="utf-8")
        written.append(path)

    def page(title: str, body: str) -> str:
        return _page(title, body, has_feed=has_feed, repo=repo)

    for position, day in enumerate(days):
        previous = days[position - 1] if position > 0 else None
        following = days[position + 1] if position + 1 < len(days) else None
        write(
            day.page, page(f"AI Radar · {day.day.isoformat()}", _day_body(day, previous, following))
        )

    if days:
        latest = days[-1]
        previous = days[-2] if len(days) > 1 else None
        write(
            "index.html",
            page(f"AI Radar · {latest.day.isoformat()}", _day_body(latest, previous, None)),
        )
    else:
        empty = "<h1>AI Radar</h1><p>No digests yet. Run <code>uv run ai-radar daily</code> to make the first one.</p>"
        write("index.html", page("AI Radar", empty))
    write("digests.html", page("AI Radar · Digests", _digests_body(days)))
    if has_feed and feed is not None:
        shutil.copyfile(feed, out_dir / FEED_NAME)
        written.append(out_dir / FEED_NAME)
    write(".nojekyll", "")  # serve the files as they are
    return written
