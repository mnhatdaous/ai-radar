"""SQLite store: items, cross-source mentions and run history.

The database is a rebuildable cache. The durable record is data/items/*.jsonl, which
``sync_from_archive`` reads back, so a fresh checkout (a CI runner, a new laptop) loses nothing.
"""

from __future__ import annotations

import gzip
import json
import logging
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from ai_radar.models import Item, Mention, from_iso, item_from_dict, to_iso

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id             TEXT PRIMARY KEY,
    url            TEXT NOT NULL,
    canonical_url  TEXT NOT NULL UNIQUE,
    title          TEXT NOT NULL,
    summary        TEXT NOT NULL DEFAULT '',
    author         TEXT,
    published_at   TEXT,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    source_name    TEXT NOT NULL,
    kind           TEXT NOT NULL,
    weight         REAL NOT NULL DEFAULT 0.5,
    topics         TEXT NOT NULL DEFAULT '[]',
    tags           TEXT NOT NULL DEFAULT '[]',
    metrics        TEXT NOT NULL DEFAULT '{}',
    engagement     REAL NOT NULL DEFAULT 0,
    discussion_url TEXT,
    domain         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_items_first_seen ON items (first_seen_at);
CREATE INDEX IF NOT EXISTS idx_items_published ON items (published_at);
CREATE INDEX IF NOT EXISTS idx_items_kind ON items (kind);
CREATE INDEX IF NOT EXISTS idx_items_source ON items (source_id);

CREATE TABLE IF NOT EXISTS mentions (
    item_id     TEXT NOT NULL REFERENCES items (id) ON DELETE CASCADE,
    source_id   TEXT NOT NULL,
    source_name TEXT NOT NULL,
    url         TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    metrics     TEXT NOT NULL DEFAULT '{}',
    seen_at     TEXT NOT NULL,
    PRIMARY KEY (item_id, source_id, url)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    new_items   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS source_runs (
    run_id      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    source_name TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    fetched     INTEGER NOT NULL DEFAULT 0,
    kept        INTEGER NOT NULL DEFAULT 0,
    new         INTEGER NOT NULL DEFAULT 0,
    seconds     REAL NOT NULL DEFAULT 0,
    error       TEXT,
    warnings    TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id, source_id)
);
PRAGMA user_version = 1;
"""

_COLUMNS = (
    "id, url, canonical_url, title, summary, author, published_at, first_seen_at, last_seen_at, "
    "source_id, source_name, kind, weight, topics, tags, metrics, engagement, discussion_url, domain"
)
_INSERT = f"INSERT INTO items ({_COLUMNS}) VALUES ({', '.join(':' + c.strip() for c in _COLUMNS.split(','))})"
_UPDATE = """
UPDATE items SET url = :url, title = :title, summary = :summary, author = :author,
    published_at = :published_at, last_seen_at = :last_seen_at, source_id = :source_id,
    source_name = :source_name, kind = :kind, weight = :weight, topics = :topics, tags = :tags,
    metrics = :metrics, engagement = :engagement, discussion_url = :discussion_url, domain = :domain
WHERE id = :id
"""


def _params(item: Item) -> dict[str, Any]:
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
        "topics": json.dumps(item.topics),
        "tags": json.dumps(item.tags),
        "metrics": json.dumps(item.metrics),
        "engagement": item.engagement,
        "discussion_url": item.discussion_url,
        "domain": item.domain,
    }


def _row_to_item(row: sqlite3.Row) -> Item:
    first_seen = from_iso(row["first_seen_at"])
    assert first_seen is not None
    return Item(
        id=row["id"],
        url=row["url"],
        canonical_url=row["canonical_url"],
        title=row["title"],
        summary=row["summary"],
        author=row["author"],
        published_at=from_iso(row["published_at"]),
        first_seen_at=first_seen,
        last_seen_at=from_iso(row["last_seen_at"]) or first_seen,
        source_id=row["source_id"],
        source_name=row["source_name"],
        kind=row["kind"],
        weight=row["weight"],
        topics=json.loads(row["topics"]),
        tags=json.loads(row["tags"]),
        metrics=json.loads(row["metrics"]),
        engagement=row["engagement"],
        discussion_url=row["discussion_url"],
        domain=row["domain"],
    )


def _union(*lists: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for values in lists:
        for value in values:
            seen.setdefault(value, None)
    return list(seen)


def _read_lines(path: Path) -> list[str]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return handle.read().splitlines()
    return path.read_text(encoding="utf-8").splitlines()


def _line_id(line: str) -> str | None:
    """The id is the first key of every archived record; reading it spares parsing known lines."""
    prefix = '{"id":"'
    if line.startswith(prefix):
        end = line.find('"', len(prefix))
        if end > 0:
            return line[len(prefix) : end]
    return None


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class Store:
    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ---- writing -----------------------------------------------------------------------

    def known_ids(self) -> set[str]:
        return {row[0] for row in self.db.execute("SELECT id FROM items")}

    def upsert_many(self, items: Iterable[Item]) -> list[bool]:
        """Insert or merge items in one transaction. True means the item was new."""
        with self.db:
            return [self._upsert(item) for item in items]

    def upsert(self, item: Item) -> bool:
        return self.upsert_many([item])[0]

    def _upsert(self, item: Item) -> bool:
        row = self.db.execute("SELECT * FROM items WHERE id = ?", (item.id,)).fetchone()
        if row is None:
            self.db.execute(_INSERT, _params(item))
            self._write_mentions(item.id, item.mentions, item.first_seen_at)
            return True

        old = _row_to_item(row)
        promote = item.weight > old.weight + 1e-9  # a more trusted source takes over the item
        best, other = (item, old) if promote else (old, item)
        loser = old if promote else item
        mentions = list(item.mentions)
        if loser.source_id != best.source_id:
            mentions.append(
                Mention(
                    loser.source_id,
                    loser.source_name,
                    loser.discussion_url or loser.url,
                    loser.title,
                    loser.metrics,
                    loser.first_seen_at,
                )
            )
        published = min((d for d in (old.published_at, item.published_at) if d), default=None)
        metrics = {
            key: max(old.metrics.get(key, 0.0), item.metrics.get(key, 0.0))
            for key in {*old.metrics, *item.metrics}
        }
        self.db.execute(
            _UPDATE,
            {
                "id": item.id,
                "url": best.url,
                "title": best.title,
                "summary": best.summary or other.summary,
                "author": best.author or other.author,
                "published_at": to_iso(published),
                "last_seen_at": to_iso(max(old.last_seen_at, item.last_seen_at)),
                "source_id": best.source_id,
                "source_name": best.source_name,
                "kind": best.kind,
                "weight": best.weight,
                "topics": json.dumps(_union(best.topics, other.topics)),
                "tags": json.dumps(_union(best.tags, other.tags)),
                "metrics": json.dumps(metrics),
                "engagement": max(old.engagement, item.engagement),
                "discussion_url": old.discussion_url or item.discussion_url,
                "domain": best.domain,
            },
        )
        self._write_mentions(item.id, mentions, item.last_seen_at)
        return False

    def _write_mentions(
        self, item_id: str, mentions: Iterable[Mention], default_seen: datetime
    ) -> None:
        for m in mentions:
            self.db.execute(
                "INSERT OR REPLACE INTO mentions "
                "(item_id, source_id, source_name, url, title, metrics, seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    m.source_id,
                    m.source_name,
                    m.url,
                    m.title,
                    json.dumps(m.metrics),
                    to_iso(m.seen_at or default_seen),
                ),
            )

    def record_run(
        self,
        *,
        run_id: str,
        started_at: datetime,
        finished_at: datetime,
        new_items: int,
        sources: Iterable[dict[str, Any]],
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO runs (run_id, started_at, finished_at, new_items) "
                "VALUES (?, ?, ?, ?)",
                (run_id, to_iso(started_at), to_iso(finished_at), new_items),
            )
            for s in sources:
                self.db.execute(
                    "INSERT OR REPLACE INTO source_runs (run_id, source_id, source_name, started_at, "
                    "ok, fetched, kept, new, seconds, error, warnings) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        s["source_id"],
                        s["source_name"],
                        to_iso(s["started_at"]),
                        int(s["ok"]),
                        s["fetched"],
                        s["kept"],
                        s["new"],
                        s["seconds"],
                        s["error"],
                        json.dumps(s["warnings"]),
                    ),
                )

    def sync_from_archive(self, items_dir: Path) -> int:
        """Load archived items this database does not know yet. Returns how many were added."""
        if not items_dir.is_dir():
            return 0
        known = self.known_ids()
        added = 0
        for path in sorted([*items_dir.glob("*.jsonl"), *items_dir.glob("*.jsonl.gz")]):
            batch: list[Item] = []
            for number, line in enumerate(_read_lines(path), start=1):
                if not line.strip() or _line_id(line) in known:
                    continue
                try:
                    item = item_from_dict(json.loads(line))
                except (ValueError, KeyError, TypeError) as exc:
                    log.warning("skipping %s line %d: %s", path.name, number, exc)
                    continue
                if item.id not in known:
                    known.add(item.id)
                    batch.append(item)
            if batch:
                self.upsert_many(batch)
                added += len(batch)
        return added

    # ---- reading -----------------------------------------------------------------------

    def _attach_mentions(self, items: list[Item]) -> list[Item]:
        by_id = {item.id: item for item in items}
        ids = list(by_id)
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ",".join("?" * len(chunk))
            rows = self.db.execute(
                f"SELECT * FROM mentions WHERE item_id IN ({marks}) ORDER BY seen_at", chunk
            )
            for row in rows:
                by_id[row["item_id"]].mentions.append(
                    Mention(
                        source_id=row["source_id"],
                        source_name=row["source_name"],
                        url=row["url"],
                        title=row["title"],
                        metrics=json.loads(row["metrics"]),
                        seen_at=from_iso(row["seen_at"]),
                    )
                )
        return items

    def items_first_seen_between(self, start: datetime, end: datetime) -> list[Item]:
        rows = self.db.execute(
            "SELECT * FROM items WHERE first_seen_at >= ? AND first_seen_at < ? "
            "ORDER BY first_seen_at, id",
            (to_iso(start), to_iso(end)),
        )
        return self._attach_mentions([_row_to_item(r) for r in rows])

    def search(
        self,
        text: str,
        *,
        kind: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
    ) -> list[Item]:
        clauses: list[str] = []
        params: list[Any] = []
        for term in text.split():
            clauses.append("(title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\')")
            params += [_like(term), _like(term)]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if since:
            clauses.append("COALESCE(published_at, first_seen_at) >= ?")
            params.append(to_iso(since))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.execute(
            f"SELECT * FROM items{where} ORDER BY COALESCE(published_at, first_seen_at) DESC LIMIT ?",
            [*params, limit],
        )
        return self._attach_mentions([_row_to_item(r) for r in rows])

    def source_health(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Latest outcome per source within [start, end)."""
        rows = self.db.execute(
            "SELECT sr.* FROM source_runs sr JOIN ("
            "  SELECT source_id, MAX(started_at) AS latest FROM source_runs "
            "  WHERE started_at >= ? AND started_at < ? GROUP BY source_id"
            ") t ON sr.source_id = t.source_id AND sr.started_at = t.latest "
            "ORDER BY sr.source_id",
            (to_iso(start), to_iso(end)),
        )
        out = []
        for row in rows:
            data = dict(row)
            data["ok"] = bool(data["ok"])
            data["warnings"] = json.loads(data["warnings"])
            out.append(data)
        return out

    def stats(self) -> dict[str, Any]:
        one = self.db.execute
        return {
            "items": one("SELECT COUNT(*) FROM items").fetchone()[0],
            "mentions": one("SELECT COUNT(*) FROM mentions").fetchone()[0],
            "runs": one("SELECT COUNT(*) FROM runs").fetchone()[0],
            "first_seen": one("SELECT MIN(first_seen_at) FROM items").fetchone()[0],
            "last_seen": one("SELECT MAX(first_seen_at) FROM items").fetchone()[0],
            "by_kind": dict(
                one("SELECT kind, COUNT(*) FROM items GROUP BY kind ORDER BY 2 DESC").fetchall()
            ),
            "top_sources": [
                tuple(r)
                for r in one(
                    "SELECT source_name, COUNT(*) FROM items GROUP BY source_id ORDER BY 2 DESC LIMIT 12"
                )
            ],
        }
