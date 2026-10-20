from datetime import datetime, timezone
from pathlib import Path

from daily_digest import ingest as ingest_module
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


def serve_fixture(url: str):
    return 200, FIXTURE


def feed_xml(*items: tuple[str, str, str]) -> bytes:
    """Build a feed from (title, link, description) tuples, all published on 14 Oct 2026."""
    body = "".join(
        f"<item><title>{t}</title><link>{link}</link>"
        f"<pubDate>Wed, 14 Oct 2026 14:00:00 GMT</pubDate><description>{d}</description></item>"
        for t, link, d in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'.encode()


def test_ingest_stores_new_items_and_removes_duplicates():
    store = Store(":memory:")
    r = ingest(make_sources("good"), serve_fixture, allow_all(), store)[0]
    assert r.error is None
    assert r.entries == 6
    assert r.skipped == 1  # the undated item
    assert r.duplicates == 2  # same URL after canonicalizing, and same content hash
    assert r.stored == 3
    assert store.count() == 3


def test_stored_items_are_clean_plain_text():
    store = Store(":memory:")
    ingest(make_sources("good"), serve_fixture, allow_all(), store)
    items = store.items_published_between(
        datetime(2026, 10, 14, tzinfo=timezone.utc), datetime(2026, 10, 15, tzinfo=timezone.utc)
    )
    bank = next(i for i in items if "Bank" in i.title)
    assert bank.canonical_url == "https://example.com/news/bank-ai"
    assert "<" not in bank.extract and "alert" not in bank.extract


def test_running_twice_adds_nothing_new():
    store = Store(":memory:")
    ingest(make_sources("good"), serve_fixture, allow_all(), store)
    second = ingest(make_sources("good"), serve_fixture, allow_all(), store)[0]
    assert second.stored == 0
    assert store.count() == 3


def test_one_failing_feed_does_not_stop_the_others():
    def fetch(url: str):
        if "bad" in url:
            raise FetchError(ErrorCode.FEED_UNREACHABLE, "timeout")
        return 200, FIXTURE

    reports = ingest(make_sources("bad", "good"), fetch, allow_all(), Store(":memory:"))
    assert reports[0].error == ErrorCode.FEED_UNREACHABLE
    assert reports[1].error is None and reports[1].stored == 3


def test_robots_disallowed_feed_is_not_fetched():
    called = []

    def fetch(url: str):
        called.append(url)
        if url.endswith("/robots.txt"):
            return 200, b"User-agent: *\nDisallow: /feed"
        return 200, FIXTURE

    reports = ingest(make_sources("blocked"), fetch, RobotsChecker(fetch, "test"), Store(":memory:"))
    assert reports[0].error == ErrorCode.ROBOTS_DISALLOWED
    assert not any(u.endswith("/feed") for u in called)


def test_invalid_feed_is_reported():
    reports = ingest(make_sources("junk"), lambda url: (200, b"not a feed at all"), allow_all(), Store(":memory:"))
    assert reports[0].error == ErrorCode.FEED_INVALID


def test_http_error_status_is_reported():
    reports = ingest(make_sources("gone"), lambda url: (500, b""), allow_all(), Store(":memory:"))
    assert reports[0].error == ErrorCode.FEED_UNREACHABLE


def test_entry_with_a_very_long_unbroken_token_is_stored():
    body = feed_xml(("Long", "https://example.com/long", "あ" * 3000), ("Normal", "https://example.com/n", "ok"))
    r = ingest(make_sources("feed"), lambda url: (200, body), allow_all(), Store(":memory:"))[0]
    assert (r.stored, r.skipped) == (2, 0)


def test_entry_that_fails_validation_is_skipped_not_fatal(monkeypatch):
    monkeypatch.setattr(ingest_module, "make_extract", lambda text: "x" * 5000)
    body = feed_xml(("A", "https://example.com/a", "one"), ("B", "https://example.com/b", "two"))
    reports = ingest(make_sources("feed", "other"), lambda url: (200, body), allow_all(), Store(":memory:"))
    assert [(r.stored, r.skipped) for r in reports] == [(0, 2), (0, 2)]


def test_headline_only_items_with_the_same_title_are_not_merged():
    body = feed_xml(
        ("Weekly roundup", "https://example.com/week-1", ""),
        ("Weekly roundup", "https://example.com/week-2", ""),
    )
    r = ingest(make_sources("feed"), lambda url: (200, body), allow_all(), Store(":memory:"))[0]
    assert (r.stored, r.duplicates) == (2, 0)


def test_long_feed_body_is_flagged_because_the_extract_is_cut():
    long_body = "A complete sentence about enterprise AI adoption. " * 40  # about 2,000 characters
    exact = ("Word " * 400)[:1200]
    store = Store(":memory:")
    feed = feed_xml(
        ("Long", "https://l.example.com/long", long_body),
        ("Exact", "https://l.example.com/exact", exact),
    )
    ingest(make_sources("l"), lambda url: (200, feed), allow_all(), store)
    assert store.count() == 2
    assert [url for _, url in store.pending_extraction()] == ["https://l.example.com/long"]
