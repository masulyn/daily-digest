"""Resource-abuse cases from PRD FR-9.4 that no other test covered: an entity bomb and a slow-drip response."""

import time

import httpx
import pytest

from daily_digest import fetch as fetch_module
from daily_digest.contracts import ErrorCode, Source
from daily_digest.fetch import TOTAL_SECONDS, FetchError, HttpFetcher
from daily_digest.ingest import ingest
from daily_digest.store import Store
from test_ingest import allow_all


def entity_bomb() -> bytes:
    entities = '<!ENTITY lol "lol">' + "".join(
        f'<!ENTITY lol{n} "{f"&lol{n - 1};" * 10 if n > 1 else "&lol;" * 10}">' for n in range(2, 10)
    )
    return (
        f'<?xml version="1.0"?><!DOCTYPE lolz [{entities}]><rss version="2.0"><channel><title>t</title><item>'
        "<title>&lol9;</title><link>https://a.example.com/x</link><pubDate>Wed, 14 Oct 2026 14:00:00 GMT</pubDate>"
        "<description>&lol9;</description></item></channel></rss>"
    ).encode()


def test_an_entity_expansion_bomb_in_a_feed_is_bounded_and_the_run_continues():
    store = Store(":memory:")
    sources = [Source(name="Bomb", url="https://a.example.com/feed", category="c"),
               Source(name="Fine", url="https://b.example.com/feed", category="c")]
    fine = entity_bomb().replace(b"&lol9;", b"plain").replace(b"a.example.com/x", b"b.example.com/y")
    started = time.monotonic()
    reports = ingest(sources, lambda url: (200, entity_bomb() if "a.example" in url else fine), allow_all(), store)
    assert time.monotonic() - started < 5
    assert reports[1].stored == 1 and reports[1].error is None  # the next feed still ran
    titles = [i.title for i in store.items_published_between(*_wide())]
    assert all(len(t) < 100 for t in titles)  # the entity was not expanded to the bomb's size


def _wide():
    from datetime import datetime, timezone

    return datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_a_slow_drip_response_is_cut_off_by_the_total_deadline(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(fetch_module.time, "monotonic", lambda: clock["now"])

    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(10_000):  # one byte at a time, each just inside the per-read timeout
                clock["now"] += 14.0
                yield b"x"

    fetcher = HttpFetcher(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Drip())),
        resolver=lambda host, port: ["8.8.8.8"], min_interval=0.0,
    )
    with pytest.raises(FetchError) as caught:
        fetcher.fetch("https://feeds.example.com/rss")
    assert caught.value.code == ErrorCode.FEED_UNREACHABLE and "too slow" in str(caught.value)
    assert clock["now"] - 1000.0 <= TOTAL_SECONDS + 14.0  # stopped soon after the deadline, not after 10,000 chunks


def test_a_fast_response_is_unaffected_by_the_deadline():
    fetcher = HttpFetcher(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"ok")),
                          resolver=lambda host, port: ["8.8.8.8"], min_interval=0.0)
    assert fetcher.fetch("https://feeds.example.com/rss") == (200, b"ok")
