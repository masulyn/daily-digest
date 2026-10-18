from pathlib import Path

from daily_digest.contracts import ErrorCode, Source
from daily_digest.fetch import FetchError
from daily_digest.ingest import ingest
from daily_digest.robots import RobotsChecker
from daily_digest.store import Store

FIXTURE = (Path(__file__).parent / "fixtures" / "sample_feed.xml").read_bytes()


def make_sources(*names: str) -> list[Source]:
    return [Source(name=n, url=f"https://{n}.example.com/feed", category="test") for n in names]


def allow_all() -> RobotsChecker:
    return RobotsChecker(lambda url: (404, b""), "test")


def test_ingest_stores_new_items_and_removes_duplicates():
    store = Store(":memory:")
    reports = ingest(make_sources("good"), lambda url: (200, FIXTURE), allow_all(), store)
    r = reports[0]
    assert r.error is None
    assert r.entries == 6
    assert r.skipped == 1  # the undated item
    assert r.duplicates == 2  # same URL after canonicalizing, and same content hash
    assert r.stored == 3
    assert store.count() == 3


def test_stored_items_are_clean_plain_text():
    store = Store(":memory:")
    ingest(make_sources("good"), lambda url: (200, FIXTURE), allow_all(), store)
    from datetime import datetime, timezone

    items = store.items_published_between(
        datetime(2026, 10, 14, tzinfo=timezone.utc), datetime(2026, 10, 15, tzinfo=timezone.utc)
    )
    bank = next(i for i in items if "Bank" in i.title)
    assert bank.canonical_url == "https://example.com/news/bank-ai"
    assert "<" not in bank.extract and "alert" not in bank.extract


def test_running_twice_adds_nothing_new():
    store = Store(":memory:")
    fetch = lambda url: (200, FIXTURE)  # noqa: E731
    ingest(make_sources("good"), fetch, allow_all(), store)
    second = ingest(make_sources("good"), fetch, allow_all(), store)[0]
    assert second.stored == 0
    assert store.count() == 3


def test_one_failing_feed_does_not_stop_the_others():
    def fetch(url: str):
        if "bad" in url:
            raise FetchError(ErrorCode.FEED_UNREACHABLE, "timeout")
        return 200, FIXTURE

    store = Store(":memory:")
    reports = ingest(make_sources("bad", "good"), fetch, allow_all(), store)
    assert reports[0].error == ErrorCode.FEED_UNREACHABLE
    assert reports[1].error is None and reports[1].stored == 3


def test_robots_disallowed_feed_is_not_fetched():
    called = []

    def fetch(url: str):
        called.append(url)
        if url.endswith("/robots.txt"):
            return 200, b"User-agent: *\nDisallow: /feed"
        return 200, FIXTURE

    robots = RobotsChecker(fetch, "test")
    reports = ingest(make_sources("blocked"), fetch, robots, Store(":memory:"))
    assert reports[0].error == ErrorCode.ROBOTS_DISALLOWED
    assert not any(u.endswith("/feed") for u in called)


def test_invalid_feed_is_reported():
    reports = ingest(make_sources("junk"), lambda url: (200, b"not a feed at all"), allow_all(), Store(":memory:"))
    assert reports[0].error == ErrorCode.FEED_INVALID


def test_http_error_status_is_reported():
    reports = ingest(make_sources("gone"), lambda url: (500, b""), allow_all(), Store(":memory:"))
    assert reports[0].error == ErrorCode.FEED_UNREACHABLE
