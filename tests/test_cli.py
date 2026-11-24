import functools
import shutil
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from ai_radar import cli, pipeline
from helpers import ROOT, Router, rss

SOURCES = """
settings: {retries: 0, backoff_base: 0}
sources:
  - {id: lab, name: Lab, type: rss, url: "https://lab.test/feed.xml", kind: blog, weight: 1.0}
"""
TWO_SOURCES = (
    SOURCES + '  - {id: other, name: Other, type: rss, url: "https://other.test/feed.xml"}\n'
)


def make_root(tmp_path: Path, sources: str = SOURCES) -> Path:
    (tmp_path / "config").mkdir(parents=True)
    shutil.copy(ROOT / "config" / "topics.yaml", tmp_path / "config" / "topics.yaml")
    (tmp_path / "config" / "sources.yaml").write_text(sources, encoding="utf-8")
    return tmp_path


def go_offline(monkeypatch: pytest.MonkeyPatch, router: Router) -> None:
    transport = httpx.MockTransport(router)
    monkeypatch.setattr(
        cli, "run_crawl", functools.partial(pipeline.run_crawl, transport=transport)
    )


def lab_feed() -> str:
    recent = format_datetime(datetime.now(UTC) - timedelta(hours=1), usegmt=True)
    return rss(
        {
            "title": "Lab ships a new LLM",
            "link": "https://lab.test/post",
            "date": recent,
            "desc": "Details",
        }
    )


def run_cli(root: Path, *args: str) -> int:
    return cli.main(["--root", str(root), *args])


def test_daily_writes_everything_and_is_idempotent(tmp_path, monkeypatch, capsys) -> None:
    root = make_root(tmp_path)
    go_offline(monkeypatch, Router().add("lab.test", text=lab_feed()))
    today = datetime.now(UTC).date().isoformat()
    archive = root / "data" / "items" / f"{today}.jsonl"

    assert run_cli(root, "daily") == 0
    digest = (root / "digests" / f"{today}.md").read_text()
    assert "Lab ships a new LLM" in digest
    assert (root / "digests" / "latest.md").read_text() == digest
    assert len(archive.read_text().splitlines()) == 1
    assert (root / "feeds" / "ai-radar.atom").exists()
    assert "1 new items from 1/1 sources ok" in capsys.readouterr().out

    assert run_cli(root, "daily") == 0  # a second run finds nothing new and changes nothing
    assert "0 new items" in capsys.readouterr().out
    assert len(archive.read_text().splitlines()) == 1

    for db_file in (root / "data").glob("radar.db*"):  # losing the local database loses nothing
        db_file.unlink()
    assert run_cli(root, "daily") == 0
    assert "0 new items" in capsys.readouterr().out
    assert len(archive.read_text().splitlines()) == 1

    assert run_cli(root, "search", "llm") == 0
    assert "Lab ships a new LLM" in capsys.readouterr().out
    assert run_cli(root, "stats") == 0
    assert "items 1" in capsys.readouterr().out
    assert run_cli(root, "digest", "--stdout") == 0
    assert "# AI Radar" in capsys.readouterr().out


def test_dry_run_reports_but_stores_nothing(tmp_path, monkeypatch, capsys) -> None:
    root = make_root(tmp_path)
    go_offline(monkeypatch, Router().add("lab.test", text=lab_feed()))
    assert run_cli(root, "crawl", "--dry-run", "--show", "1") == 0
    out = capsys.readouterr().out
    assert "Lab ships a new LLM" in out and "nothing was stored" in out
    assert not (root / "data").exists()


def test_exit_codes(tmp_path, monkeypatch, capsys) -> None:
    root = make_root(tmp_path, TWO_SOURCES)
    go_offline(monkeypatch, Router().add("lab.test", text=lab_feed()).add("other.test", status=500))
    assert run_cli(root, "daily") == 0  # one source down is normal life
    assert run_cli(root, "daily", "--strict") == 1
    out = capsys.readouterr().out
    assert "FAIL  other" in out

    go_offline(monkeypatch, Router().add("lab.test", status=500).add("other.test", status=500))
    assert run_cli(root, "daily") == 1  # nothing worked: tell the scheduler


def test_configuration_errors_exit_with_code_2(tmp_path, capsys) -> None:
    assert run_cli(tmp_path / "nothing-here", "sources") == 2
    assert "missing config file" in capsys.readouterr().err

    root = make_root(tmp_path / "ok")
    assert run_cli(root, "digest", "--date", "yesterday") == 2
    assert "--date" in capsys.readouterr().err
    assert run_cli(root, "crawl", "--dry-run", "--source", "nope") == 2
    assert "unknown source id" in capsys.readouterr().err


def test_the_shipped_sources_list_prints() -> None:
    assert cli.main(["--root", str(ROOT), "sources"]) == 0
