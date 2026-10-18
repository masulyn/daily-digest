import sqlite3
import time
from pathlib import Path

from daily_digest.contracts import Source
from daily_digest.fetch import HttpFetcher
from daily_digest.ingest import ingest
from daily_digest.robots import RobotsChecker
from daily_digest.store import _MIGRATIONS, Store
from helpers import make_item

FEED = (Path(__file__).parent / "fixtures" / "sample_feed.xml").read_bytes()


def test_ingest_flags_short_feed_text_for_extraction():
    store = Store(":memory:")
    source = Source(name="s", url="https://example.com/feed", category="test")
    ingest([source], lambda url: (200, FEED), RobotsChecker(lambda u: (404, b""), "t"), store)
    # every item in the fixture feed has a one-line description, so all are flagged
    assert len(store.pending_extraction()) == store.count() > 0


def test_rows_stored_before_extraction_existed_are_flagged_by_length(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(_MIGRATIONS[0])  # schema as it was at version 1
    old.executemany(
        "INSERT INTO items VALUES (?, 's', ?, ?, 't', '2026-10-14T12:00:00+00:00', ?, 'x')",
        [("thin", "https://e.com/thin", "h1", "tiny"), ("full", "https://e.com/full", "h2", "x" * 500)],
    )
    old.commit()
    old.close()
    with Store(path) as store:
        assert store.pending_extraction() == [("thin", "https://e.com/thin")]


def test_saved_text_and_failures_leave_the_pending_list():
    store = Store(":memory:")
    store.add_if_new(make_item("a"), needs_extraction=True)
    store.add_if_new(make_item("b"), needs_extraction=True)
    store.save_article_text("a", "text")
    store.record_extraction_failure("b", "NO_ARTICLE_TEXT")
    assert store.pending_extraction() == []


def test_crawl_delay_is_read_from_robots_txt():
    robots = RobotsChecker(lambda u: (200, b"User-agent: *\nCrawl-delay: 5"), "t")
    assert robots.crawl_delay("https://example.com/a") == 5.0
    assert RobotsChecker(lambda u: (404, b""), "t").crawl_delay("https://example.com/a") is None


def test_fetcher_waits_the_sites_crawl_delay(monkeypatch):
    sleeps = []
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(s))
    with HttpFetcher(min_interval=1.0) as fetcher:
        fetcher.set_host_delay("slow.example.com", 5.0)
        fetcher._wait_for_turn("slow.example.com")  # first request: no wait
        clock[0] += 1.0
        fetcher._wait_for_turn("slow.example.com")  # 1s elapsed, needs 5s
        assert sleeps == [4.0]
        fetcher._wait_for_turn("fast.example.com")  # other hosts keep the default
        assert sleeps == [4.0]
