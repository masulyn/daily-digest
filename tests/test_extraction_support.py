import subprocess
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

from daily_digest import fetch as fetch_module
from daily_digest.contracts import ErrorCode, Source
from daily_digest.fetch import HttpFetcher
from daily_digest.ingest import ingest
from daily_digest.robots import RobotsChecker
from daily_digest.store import _MIGRATIONS, Store
from helpers import sample_item

FEED = (Path(__file__).parent / "fixtures" / "sample_feed.xml").read_bytes()


def test_ingest_flags_short_feed_text_for_extraction():
    store = Store(":memory:")
    source = Source(name="s", url="https://example.com/feed", category="test")
    ingest([source], lambda url: (200, FEED), RobotsChecker(lambda u: (404, b""), "t"), store)
    # every item in the fixture feed has a one-line description, so all are flagged
    assert len(store.pending_extraction()) == store.count() > 0


def test_plain_ingest_does_not_load_the_article_parser():
    code = "import sys, daily_digest.ingest, daily_digest.cli; assert 'trafilatura' not in sys.modules"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


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


def test_saved_text_and_final_failures_leave_the_pending_list():
    store = Store(":memory:")
    store.add_if_new(sample_item("a"), needs_extraction=True)
    store.add_if_new(sample_item("b"), needs_extraction=True)
    store.save_article_text("a", "text")
    store.record_extraction_failure("b", "NO_ARTICLE_TEXT", final=True)
    assert store.pending_extraction() == []


def test_crawl_delay_is_read_from_robots_txt():
    robots = RobotsChecker(lambda u: (200, b"User-agent: *\nCrawl-delay: 5"), "t")
    assert robots.crawl_delay("https://example.com/a") == 5.0
    assert RobotsChecker(lambda u: (404, b""), "t").crawl_delay("https://example.com/a") is None


def test_fetcher_waits_the_sites_crawl_delay(monkeypatch):
    sleeps = []
    monkeypatch.setattr(fetch_module.time, "sleep", sleeps.append)
    with HttpFetcher(
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        resolver=lambda host, port: ["8.8.8.8"],
        min_interval=0.0,
    ) as fetcher:
        fetcher.set_host_delay("slow.example.com", 5.0)
        fetcher.fetch("https://slow.example.com/a")
        fetcher.fetch("https://slow.example.com/b")
        assert len(sleeps) == 1 and 4.0 < sleeps[0] <= 5.0
        fetcher.fetch("https://fast.example.com/a")  # other hosts keep the default
        assert len(sleeps) == 1


def test_fetch_asks_allow_for_every_hop_and_stops_when_refused():
    asked, requested = [], []

    def handler(request):
        requested.append(str(request.headers["Host"]))
        return httpx.Response(302, headers={"Location": "https://b.example.com/x"})

    with HttpFetcher(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["8.8.8.8"], min_interval=0.0
    ) as fetcher:
        def allow(url):
            asked.append(url)
            return "b.example.com" not in url

        with pytest.raises(fetch_module.FetchError) as e:
            fetcher.fetch("https://a.example.com/x", allow=allow)
        assert e.value.code == ErrorCode.ROBOTS_DISALLOWED
    assert asked == ["https://a.example.com/x", "https://b.example.com/x"]
    assert requested == ["a.example.com"]
