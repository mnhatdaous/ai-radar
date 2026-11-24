import gzip
import json
from datetime import timedelta

from ai_radar.export import write_day_archive
from ai_radar.models import Mention
from ai_radar.store import Store
from helpers import NOW, make_item

DAY = timedelta(days=1)


def one(store: Store):
    [stored] = store.items_first_seen_between(NOW - DAY, NOW + DAY)
    return stored


def test_insert_then_seen_again() -> None:
    store = Store()
    assert store.upsert(make_item()) is True
    assert store.upsert(make_item(last_seen_at=NOW + timedelta(hours=5))) is False
    assert store.known_ids() == {"id-1"}
    assert one(store).first_seen_at == NOW  # first sight is never overwritten


def test_a_more_trusted_source_takes_over_and_the_old_one_becomes_a_mention() -> None:
    store = Store()
    hn_thread = "https://news.ycombinator.com/item?id=1"
    store.upsert(
        make_item(
            source_id="hn",
            source_name="Hacker News",
            weight=0.5,
            title="HN title",
            kind="discussion",
            metrics={"hn_points": 100},
            discussion_url=hn_thread,
        )
    )
    store.upsert(
        make_item(
            source_id="lab",
            source_name="Lab",
            weight=1.0,
            title="Official title",
            kind="blog",
            summary="Official summary",
        )
    )
    stored = one(store)
    assert (stored.source_id, stored.title, stored.kind) == ("lab", "Official title", "blog")
    assert stored.summary == "Official summary"
    assert stored.metrics["hn_points"] == 100  # popularity is kept
    assert stored.discussion_url == hn_thread
    assert [m.source_id for m in stored.mentions] == ["hn"]
    assert stored.mentions[0].url == hn_thread


def test_a_less_trusted_source_only_adds_a_mention() -> None:
    store = Store()
    store.upsert(make_item(source_id="lab", weight=1.0, title="Official"))
    store.upsert(
        make_item(
            source_id="reddit",
            source_name="Reddit",
            weight=0.3,
            title="Reddit title",
            discussion_url="https://reddit.com/r/x/1",
            metrics={"reddit_rank": 2},
        )
    )
    stored = one(store)
    assert (stored.source_id, stored.title) == ("lab", "Official")
    assert [m.source_id for m in stored.mentions] == ["reddit"]
    assert stored.metrics == {"reddit_rank": 2}
    assert stored.discussion_url == "https://reddit.com/r/x/1"


def test_the_same_source_seen_again_updates_numbers_without_a_mention() -> None:
    store = Store()
    store.upsert(make_item(metrics={"hn_points": 10}, engagement=10, topics=["llm"]))
    store.upsert(
        make_item(
            metrics={"hn_points": 50},
            engagement=50,
            topics=["agents"],
            published_at=NOW - timedelta(hours=5),
            last_seen_at=NOW + timedelta(hours=1),
        )
    )
    stored = one(store)
    assert stored.metrics == {"hn_points": 50}
    assert stored.engagement == 50
    assert stored.topics == ["llm", "agents"]
    assert stored.published_at == NOW - timedelta(hours=5)  # the earliest date wins
    assert stored.mentions == []


def test_items_first_seen_between_is_half_open() -> None:
    store = Store()
    store.upsert_many(
        [
            make_item(id="a", canonical_url="https://e.test/a", first_seen_at=NOW),
            make_item(id="b", canonical_url="https://e.test/b", first_seen_at=NOW + DAY),
        ]
    )
    assert [i.id for i in store.items_first_seen_between(NOW, NOW + DAY)] == ["a"]
    assert [i.id for i in store.items_first_seen_between(NOW + DAY, NOW + 2 * DAY)] == ["b"]


def test_search() -> None:
    store = Store()
    store.upsert_many(
        [
            make_item(
                id="1",
                canonical_url="https://e.test/1",
                title="Claude gets faster",
                summary="inference speed",
            ),
            make_item(
                id="2",
                canonical_url="https://e.test/2",
                title="Llama weights released",
                kind="model",
                published_at=NOW - 20 * DAY,
            ),
            make_item(id="3", canonical_url="https://e.test/3", title="100% growth in GPU demand"),
        ]
    )
    ids = lambda **kw: sorted(i.id for i in store.search(**kw))  # noqa: E731
    assert ids(text="claude") == ["1"]
    assert ids(text="claude speed") == ["1"]  # every word must match, in title or summary
    assert ids(text="claude llama") == []
    assert ids(text="llama", kind="news") == []
    assert ids(text="llama", kind="model") == ["2"]
    assert ids(text="", since=NOW - 5 * DAY) == ["1", "3"]
    assert ids(text="100%") == ["3"]  # % is literal, not a wildcard
    assert ids(text="1%") == []


def test_run_history_reports_the_latest_outcome_per_source() -> None:
    store = Store()

    def source(source_id: str, ok: bool, error: str | None = None) -> dict:
        return {
            "source_id": source_id,
            "source_name": source_id.upper(),
            "started_at": NOW,
            "ok": ok,
            "fetched": 5,
            "kept": 3,
            "new": 3,
            "seconds": 1.2,
            "error": error,
            "warnings": ["one page failed"] if ok else [],
        }

    store.record_run(
        run_id="r1",
        started_at=NOW,
        finished_at=NOW,
        new_items=3,
        sources=[source("a", True), source("b", False, "HTTP 500")],
    )
    later = NOW + timedelta(hours=1)
    run2 = source("b", True)
    run2["started_at"] = later
    store.record_run(run_id="r2", started_at=later, finished_at=later, new_items=0, sources=[run2])

    health = {h["source_id"]: h for h in store.source_health(NOW - DAY, NOW + DAY)}
    assert health["a"]["ok"] is True
    assert health["b"]["ok"] is True  # the later run replaced the failure
    assert health["a"]["warnings"] == ["one page failed"]
    assert store.stats()["runs"] == 2


def test_archive_round_trip_restores_items_and_mentions(tmp_path) -> None:
    mention = Mention(
        "hn", "Hacker News", "https://news.ycombinator.com/item?id=1", "t", {"hn_points": 5}, NOW
    )
    items = [
        make_item(id="a", canonical_url="https://e.test/a", mentions=[mention]),
        make_item(id="b", canonical_url="https://e.test/b"),
    ]
    path = tmp_path / "items" / "2026-11-10.jsonl"
    assert write_day_archive(items, path) == 2

    fresh = Store()
    assert fresh.sync_from_archive(path.parent) == 2
    assert fresh.sync_from_archive(path.parent) == 0  # idempotent
    restored = {i.id: i for i in fresh.items_first_seen_between(NOW - DAY, NOW + DAY)}
    assert restored["a"].mentions[0].metrics == {"hn_points": 5}
    assert restored["b"].mentions == []


def test_archive_writes_merge_instead_of_overwriting(tmp_path) -> None:
    path = tmp_path / "2026-11-10.jsonl"
    write_day_archive([make_item(id="a", canonical_url="https://e.test/a", title="First")], path)
    write_day_archive(
        [make_item(id="b", canonical_url="https://e.test/b")], path
    )  # another machine
    write_day_archive([make_item(id="a", canonical_url="https://e.test/a", title="Updated")], path)
    records = {r["id"]: r for r in map(json.loads, path.read_text().splitlines())}
    assert set(records) == {"a", "b"}
    assert records["a"]["title"] == "Updated"


def test_gzipped_archives_are_read_too(tmp_path) -> None:
    plain = tmp_path / "2026-01-01.jsonl"
    write_day_archive([make_item(id="a", canonical_url="https://e.test/a")], plain)
    with gzip.open(tmp_path / "2026-01-01.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(plain.read_text())
    plain.unlink()
    assert Store().sync_from_archive(tmp_path) == 1


def test_malformed_archive_lines_are_skipped(tmp_path) -> None:
    good = tmp_path / "good.jsonl"
    write_day_archive([make_item(id="a", canonical_url="https://e.test/a")], good)
    (tmp_path / "2026-11-11.jsonl").write_text(
        good.read_text() + "this is not json\n" + '{"id": "x"}\n' + "\n", encoding="utf-8"
    )
    good.unlink()
    assert Store().sync_from_archive(tmp_path) == 1


def test_archive_lines_the_database_already_knows_are_not_parsed(tmp_path, monkeypatch) -> None:
    write_day_archive(
        [make_item(id="a", canonical_url="https://e.test/a")], tmp_path / "2026-11-10.jsonl"
    )
    store = Store()
    assert store.sync_from_archive(tmp_path) == 1

    def explode(_data):
        raise AssertionError("a known record was parsed")

    monkeypatch.setattr("ai_radar.store.item_from_dict", explode)
    assert store.sync_from_archive(tmp_path) == 0
