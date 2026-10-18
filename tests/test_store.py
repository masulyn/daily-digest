import sqlite3
from datetime import date, datetime, timezone

import pytest

from daily_digest.contracts import CandidateItem
from daily_digest.normalize import content_hash, item_id
from daily_digest.store import Store

DAY_START = datetime(2026, 10, 14, tzinfo=timezone.utc)
DAY_END = datetime(2026, 10, 15, tzinfo=timezone.utc)


def make_item(slug: str, title: str | None = None) -> CandidateItem:
    url = f"https://example.com/{slug}"
    title = title or f"Story {slug}"
    return CandidateItem(
        id=item_id(url),
        source="test",
        canonical_url=url,
        title=title,
        published=datetime(2026, 10, 14, 12, tzinfo=timezone.utc),
        extract="text",
        content_hash=content_hash(title, "text"),
    )


def test_duplicate_url_or_content_is_rejected():
    store = Store(":memory:")
    assert store.add_if_new(make_item("a"))
    assert not store.add_if_new(make_item("a"))
    assert not store.add_if_new(make_item("b", title="Story a"))  # same content, new URL
    assert store.count() == 1


def test_transaction_rolls_back_on_error():
    store = Store(":memory:")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.add_if_new(make_item("a"))
            raise RuntimeError("boom")
    assert store.count() == 0


def test_transaction_commits_on_success_and_nests():
    store = Store(":memory:")
    with store.transaction():
        store.add_if_new(make_item("a"))
        store.mark_seen([make_item("a")], date(2026, 10, 14))  # joins the outer transaction
    assert store.count() == 1


def test_data_survives_reopening(tmp_path):
    path = tmp_path / "nested" / "digest.db"
    with Store(path) as store:
        store.add_if_new(make_item("a"))
    with Store(path) as store:
        assert store.count() == 1


def test_seen_items_are_excluded_by_url_or_content_until_requested():
    store = Store(":memory:")
    for slug in ("a", "b"):
        store.add_if_new(make_item(slug))
    store.mark_seen([make_item("a")], date(2026, 10, 14))

    unseen = store.items_published_between(DAY_START, DAY_END)
    assert [i.canonical_url for i in unseen] == ["https://example.com/b"]
    everything = store.items_published_between(DAY_START, DAY_END, include_seen=True)
    assert len(everything) == 2


def test_items_outside_the_window_are_excluded():
    store = Store(":memory:")
    store.add_if_new(make_item("a"))
    assert store.items_published_between(DAY_END, datetime(2026, 10, 16, tzinfo=timezone.utc)) == []


def test_new_database_is_versioned():
    store = Store(":memory:")
    assert store._db.execute("PRAGMA user_version").fetchone()[0] == 1


def test_database_from_before_versioning_is_upgraded(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE items (
            id TEXT PRIMARY KEY, source TEXT NOT NULL, canonical_url TEXT NOT NULL UNIQUE,
            content_hash TEXT NOT NULL, title TEXT NOT NULL, published_at TEXT NOT NULL,
            extract TEXT NOT NULL, first_seen_at TEXT NOT NULL
        );
        CREATE INDEX idx_items_hash ON items(content_hash);
        INSERT INTO items VALUES ('1', 's', 'https://example.com/a', 'h', 't',
                                  '2026-10-14T12:00:00+00:00', 'x', '2026-10-14T13:00:00+00:00');
        """
    )
    old.commit()
    old.close()

    with Store(path) as store:
        assert store.count() == 1  # existing rows are kept
        store.mark_seen([make_item("z")], date(2026, 10, 14))  # new table exists
        assert not store.add_if_new(make_item("dup", title="t").model_copy(update={"content_hash": "h"}))
