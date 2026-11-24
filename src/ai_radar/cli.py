"""Command line interface: ``ai-radar daily`` is the one you schedule."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from ai_radar import __version__
from ai_radar.config import AppConfig, ConfigError, Paths, load_config, resolve_root
from ai_radar.digest import render_digest
from ai_radar.export import feed_selection, write_atom, write_day_archive, write_text
from ai_radar.pipeline import RunReport, run_crawl
from ai_radar.site import FEED_NAME, build_site
from ai_radar.store import Store

log = logging.getLogger("ai_radar")


# ---- shared helpers ---------------------------------------------------------------------


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def write_outputs(
    store: Store, cfg: AppConfig, paths: Paths, *, day: date, now: datetime
) -> dict[str, Path | int]:
    """Digest, JSONL archive and Atom feed for ``day``. Safe to re-run: same input, same files."""
    start, end = _day_bounds(day)
    items = store.items_first_seen_between(start, end)
    markdown = render_digest(
        items,
        day=day,
        now=min(now, end),
        settings=cfg.settings.digest,
        health=store.source_health(start, end),
    )
    digest_path = paths.digests_dir / f"{day.isoformat()}.md"
    write_text(digest_path, markdown)
    if day == now.date():
        write_text(paths.digests_dir / "latest.md", markdown)
    archived = write_day_archive(items, paths.items_dir / f"{day.isoformat()}.jsonl")
    recent = store.items_first_seen_between(now - timedelta(days=7), now + timedelta(seconds=1))
    feed_path = paths.feeds_dir / "ai-radar.atom"
    write_atom(feed_selection(recent, now, settings=cfg.settings.digest), feed_path, now=now)
    return {
        "digest": digest_path,
        "archive": paths.items_dir / f"{day.isoformat()}.jsonl",
        "archived": archived,
        "feed": feed_path,
        "items": len(items),
    }


def print_report(report: RunReport, *, show: int = 0) -> None:
    for r in report.results:
        if r.ok:
            line = f"  ok    {r.source_id:<28} fetched {r.fetched:>4}  kept {r.kept:>4}  new {r.new:>4}  {r.seconds:>5.1f}s"
            if r.warnings:
                line += f"   (partial: {r.warnings[0][:70]})"
            print(line)
        else:
            print(f"  FAIL  {r.source_id:<28} {r.error}")
        if show:
            for item in [i for i in report.items if i.source_id == r.source_id][:show]:
                print(f"          · {item.title[:100]}  ({item.domain})")
    ok = len(report.results) - len(report.failed)
    print(f"\n{report.new_items} new items from {ok}/{len(report.results)} sources ok")


def _exit_code(report: RunReport, strict: bool) -> int:
    if report.results and len(report.failed) == len(report.results):
        return 1  # nothing worked: surface it to the scheduler
    return 1 if strict and report.failed else 0


def _parse_day(value: str | None, now: datetime) -> date:
    try:
        return date.fromisoformat(value) if value else now.date()
    except ValueError as exc:
        raise ConfigError(f"--date must look like 2026-11-10, got {value!r}") from exc


# ---- commands ---------------------------------------------------------------------------


def cmd_daily(args: argparse.Namespace) -> int:
    paths = Paths(resolve_root(args.root))
    cfg = load_config(paths.config_dir)
    now = datetime.now(UTC).replace(microsecond=0)
    with Store(paths.db) as store:
        restored = store.sync_from_archive(paths.items_dir)
        if restored:
            log.info("restored %d items from the archive", restored)
        report = asyncio.run(
            run_crawl(
                cfg,
                store,
                now=now,
                only=args.source,
                skip=args.skip,
                max_age_days=args.max_age_days,
            )
        )
        out = write_outputs(store, cfg, paths, day=now.date(), now=now)
    print_report(report)
    print(f"\ndigest   {_rel(out['digest'], paths.root)}  ({out['items']} items today)")  # type: ignore[arg-type]
    print(f"archive  {_rel(out['archive'], paths.root)}  ({out['archived']} records)")  # type: ignore[arg-type]
    print(f"feed     {_rel(out['feed'], paths.root)}")  # type: ignore[arg-type]
    return _exit_code(report, args.strict)


def cmd_crawl(args: argparse.Namespace) -> int:
    paths = Paths(resolve_root(args.root))
    cfg = load_config(paths.config_dir)
    store = Store(paths.db) if not args.dry_run or paths.db.exists() else None
    try:
        if store is not None and not args.dry_run:
            store.sync_from_archive(paths.items_dir)
        report = asyncio.run(
            run_crawl(
                cfg,
                store,
                only=args.source,
                skip=args.skip,
                dry_run=args.dry_run,
                max_age_days=args.max_age_days,
            )
        )
    finally:
        if store is not None:
            store.close()
    print_report(report, show=args.show)
    if args.dry_run:
        print("(dry run: nothing was stored)")
    return _exit_code(report, args.strict)


def cmd_digest(args: argparse.Namespace) -> int:
    paths = Paths(resolve_root(args.root))
    cfg = load_config(paths.config_dir)
    now = datetime.now(UTC).replace(microsecond=0)
    day = _parse_day(args.date, now)
    with Store(paths.db) as store:
        store.sync_from_archive(paths.items_dir)
        if args.stdout:
            start, end = _day_bounds(day)
            sys.stdout.write(
                render_digest(
                    store.items_first_seen_between(start, end),
                    day=day,
                    now=min(now, end),
                    settings=cfg.settings.digest,
                    health=store.source_health(start, end),
                )
            )
            return 0
        out = write_outputs(store, cfg, paths, day=day, now=now)
    print(f"digest   {_rel(out['digest'], paths.root)}  ({out['items']} items)")  # type: ignore[arg-type]
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    cfg = load_config(Paths(resolve_root(args.root)).config_dir)
    print(f"{'id':<28} {'type':<16} {'kind':<11} {'weight':>6}  filter  state")
    for s in cfg.sources:
        state = "on" if s.enabled else "off"
        print(f"{s.id:<28} {s.type:<16} {s.kind:<11} {s.weight:>6.1f}  {s.ai_filter:<6}  {state}")
    print(f"\n{sum(s.enabled for s in cfg.sources)} of {len(cfg.sources)} sources enabled")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    paths = Paths(resolve_root(args.root))
    since = datetime.now(UTC) - timedelta(days=args.days) if args.days else None
    with Store(paths.db) as store:
        store.sync_from_archive(paths.items_dir)
        hits = store.search(" ".join(args.query), kind=args.kind, since=since, limit=args.limit)
    for item in hits:
        when = (item.published_at or item.first_seen_at).strftime("%Y-%m-%d")
        print(
            f"{when}  {item.kind:<10} {item.title[:90]}\n            {item.url}  [{item.source_name}]"
        )
    print(f"\n{len(hits)} result(s)")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    paths = Paths(resolve_root(args.root))
    with Store(paths.db) as store:
        store.sync_from_archive(paths.items_dir)
        stats = store.stats()
    print(f"items {stats['items']}  mentions {stats['mentions']}  runs {stats['runs']}")
    print(f"first seen {stats['first_seen']}  last seen {stats['last_seen']}")
    print("by kind:   " + ", ".join(f"{k} {n}" for k, n in stats["by_kind"].items()))
    print("top sources:")
    for name, count in stats["top_sources"]:
        print(f"  {count:>6}  {name}")
    return 0


def cmd_site(args: argparse.Namespace) -> int:
    paths = Paths(resolve_root(args.root))
    out = Path(args.out).expanduser() if args.out else paths.root / "site"
    written = build_site(
        paths.digests_dir,
        out,
        feed=paths.feeds_dir / FEED_NAME,
        repo=args.repo or os.environ.get("GITHUB_REPOSITORY"),
    )
    pages = sum(1 for path in written if path.suffix == ".html")
    print(f"site     {_rel(out, paths.root)}  ({pages} pages)")
    return 0


# ---- entry point ------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-radar", description="Daily crawler for AI news, research, tools, models and repos."
    )
    parser.add_argument("--version", action="version", version=f"ai-radar {__version__}")
    parser.add_argument(
        "--root", help="repo root holding config/ and data/ (default: $AI_RADAR_ROOT or cwd)"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def crawl_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--source", action="append", metavar="ID", help="only this source (repeatable)"
        )
        p.add_argument(
            "--skip",
            action="append",
            metavar="ID",
            help="leave this source out of the run (repeatable), e.g. one that blocks CI servers",
        )
        p.add_argument(
            "--max-age-days",
            type=int,
            metavar="N",
            help="override the age window, e.g. 30 to backfill",
        )
        p.add_argument("--strict", action="store_true", help="exit 1 if any source failed")

    p = sub.add_parser(
        "daily", help="crawl, then write today's digest, archive and feed (schedule this)"
    )
    crawl_flags(p)
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("crawl", help="crawl and store, without writing a digest")
    crawl_flags(p)
    p.add_argument("--dry-run", action="store_true", help="fetch and report, store nothing")
    p.add_argument("--show", type=int, default=0, metavar="N", help="print N titles per source")
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser(
        "digest", help="(re)build the digest, archive and feed for a day from stored items"
    )
    p.add_argument("--date", help="UTC day, e.g. 2026-11-10 (default: today)")
    p.add_argument(
        "--stdout", action="store_true", help="print the digest instead of writing files"
    )
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("sources", help="list configured sources")
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("search", help="search everything crawled so far")
    p.add_argument("query", nargs="+")
    p.add_argument(
        "--kind", help="news, blog, paper, repo, tool, model, dataset, release, discussion, video"
    )
    p.add_argument("--days", type=int, help="only items from the last N days")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("stats", help="what is in the store")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser(
        "site", help="build the website (digests, archive, feed) that GitHub Pages serves"
    )
    p.add_argument("--out", metavar="DIR", help="where to write it (default: ./site)")
    p.add_argument(
        "--repo", metavar="OWNER/NAME", help="link to this repo (default: $GITHUB_REPOSITORY)"
    )
    p.set_defaults(func=cmd_site)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request is noise
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
