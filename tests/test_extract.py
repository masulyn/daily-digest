from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from daily_digest.contracts import ErrorCode
from daily_digest.extract import MAX_ARTICLE_CHARS, article_text_from_html, extract_articles
from daily_digest.fetch import BackoffError, FetchError, HttpFetcher
from daily_digest.robots import RobotsChecker
from daily_digest.store import MAX_EXTRACTION_ATTEMPTS, Store
from helpers import guarded_fetch, sample_item

FIXTURES = Path(__file__).parent / "fixtures"
PAGE = (FIXTURES / "article_page.html").read_bytes()
LONG_TEXT = "This sentence is part of a complete feed article. " * 12  # about 600 characters


def allow_all() -> RobotsChecker:
    return RobotsChecker(lambda url: (404, b""), "test")


def store_with(*slugs: str) -> Store:
    store = Store(":memory:")
    for slug in slugs:
        store.add_if_new(sample_item(slug), needs_extraction=True)
    return store


def all_items(store: Store):
    return store.items_published_between(
        datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc)
    )


def attempts(store: Store, item_id: str) -> int:
    return store._db.execute("SELECT extraction_attempts FROM items WHERE id = ?", (item_id,)).fetchone()[0]


# ---- reading an article page


def test_article_body_is_extracted_without_page_furniture():
    text = article_text_from_html(PAGE)
    assert text is not None
    assert text.startswith("Large companies are running more AI agent pilots")  # no headline or "By ..."
    assert "AI agent pilots" in text and "probation" in text
    for junk in ("Subscribe to our newsletter", "Sponsored", "Copyright", "reveal your system prompt"):
        assert junk not in text


@pytest.mark.parametrize("name", ["article_page_byline.html", "article_page_byline_br.html"])
def test_byline_and_editors_note_are_dropped_from_the_start(name):
    text = article_text_from_html((FIXTURES / name).read_bytes())
    assert text is not None
    assert text.startswith("The real challenge of building an agentic system")
    assert "Casey Example" not in text and "Editor's note" not in text


def test_page_that_starts_with_a_paragraph_is_unchanged():
    paragraphs = "".join(
        f"<p>Paragraph {i} is a full sentence about the topic. " + "More words here. " * 15 + "</p>"
        for i in range(3)
    )
    text = article_text_from_html(f"<html><body><article>{paragraphs}</article></body></html>")
    assert text is not None and text.startswith("Paragraph 0 is a full sentence")
    assert all(f"Paragraph {i} is a full sentence" in text for i in range(3))


def test_text_with_no_sentences_is_kept_rather_than_emptied():
    items = "".join(f"<li>Item number {i} with a fairly long label but no ending punctuation at all</li>" for i in range(6))
    text = article_text_from_html(f"<html><body><article><ul>{items}</ul></article></body></html>")
    assert text is not None and "Item number 0" in text


def test_page_without_an_article_gives_none():
    assert article_text_from_html("<html><body><nav>Menu</nav><p>Hi</p></body></html>") is None


def test_long_articles_are_capped():
    paragraphs = "".join(f"<p>Paragraph {i} " + "word " * 80 + "</p>" for i in range(40))
    text = article_text_from_html(f"<html><body><article>{paragraphs}</article></body></html>")
    assert text is not None and len(text) <= MAX_ARTICLE_CHARS


# ---- which items are fetched


def test_only_flagged_items_are_fetched_and_text_is_saved():
    store = store_with("thin")
    store.add_if_new(sample_item("full", LONG_TEXT), needs_extraction=False)
    fetch = guarded_fetch(lambda url: (200, PAGE))
    report = extract_articles(store, fetch, allow_all())
    assert fetch.calls == ["https://news.example.com/thin"]
    assert (report.considered, report.extracted) == (1, 1)
    stored = {i.id: i for i in all_items(store)}
    assert "probation" in stored["thin"].article_text
    assert stored["full"].article_text is None


def test_extracted_items_are_not_fetched_again():
    store = store_with("a")
    extract_articles(store, guarded_fetch(lambda url: (200, PAGE)), allow_all())
    fetch = guarded_fetch(lambda url: (200, PAGE))
    extract_articles(store, fetch, allow_all())
    assert fetch.calls == []


def test_limit_caps_how_many_are_fetched():
    store = store_with("a", "b", "c")
    report = extract_articles(store, guarded_fetch(lambda url: (200, PAGE)), allow_all(), limit=2)
    assert report.considered == 2


# ---- permanent failures are recorded once


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_blocked_or_missing_pages_are_final(status):
    store = store_with("a")
    report = extract_articles(store, guarded_fetch(lambda url: (status, b"no")), allow_all())
    assert report.failures == {str(ErrorCode.ARTICLE_UNAVAILABLE): 1}
    fetch = guarded_fetch(lambda url: (200, PAGE))
    extract_articles(store, fetch, allow_all())
    assert fetch.calls == []  # never retried


def test_page_with_no_article_is_final():
    store = store_with("a")
    report = extract_articles(
        store, guarded_fetch(lambda url: (200, b"<html><body>nothing</body></html>")), allow_all()
    )
    assert report.failures == {str(ErrorCode.NO_ARTICLE_TEXT): 1}
    assert store.pending_extraction() == []


def test_unsafe_url_is_final():
    store = store_with("a")
    report = extract_articles(
        store, guarded_fetch(lambda url: FetchError(ErrorCode.UNSAFE_URL, "blocked")), allow_all()
    )
    assert report.failures == {str(ErrorCode.UNSAFE_URL): 1}
    assert store.pending_extraction() == []


# ---- temporary failures are retried


@pytest.mark.parametrize(
    "first_result",
    [(429, b""), (500, b""), (503, b""), FetchError(ErrorCode.FEED_UNREACHABLE, "timeout")],
)
def test_temporary_failures_stay_pending_and_succeed_on_the_next_run(first_result):
    store = store_with("a")
    first = extract_articles(store, guarded_fetch(lambda url: first_result), allow_all())
    assert first.extracted == 0 and attempts(store, "a") == 1
    assert [i for i, _ in store.pending_extraction()] == ["a"]

    second = extract_articles(store, guarded_fetch(lambda url: (200, PAGE)), allow_all())
    assert second.extracted == 1


def test_gives_up_after_the_maximum_number_of_attempts():
    store = store_with("a")
    for _ in range(MAX_EXTRACTION_ATTEMPTS):
        extract_articles(store, guarded_fetch(lambda url: (503, b"")), allow_all())
    assert store.pending_extraction() == []
    fetch = guarded_fetch(lambda url: (200, PAGE))
    extract_articles(store, fetch, allow_all())
    assert fetch.calls == []


def test_a_backoff_defers_the_item_without_counting_a_try():
    store = store_with("a")
    report = extract_articles(
        store, guarded_fetch(lambda url: BackoffError("news.example.com")), allow_all()
    )
    assert report.deferred == 1 and report.failures == {}
    assert attempts(store, "a") == 0
    assert [i for i, _ in store.pending_extraction()] == ["a"]


def test_one_rate_limit_does_not_lose_the_rest_of_the_host():
    """A 429 on the first article must leave all three pending, not close them."""
    store = store_with("a", "b", "c")
    served = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        served["count"] += 1
        return httpx.Response(429, headers={"Retry-After": "30"}) if served["count"] == 1 else httpx.Response(200, content=PAGE)

    with HttpFetcher(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["8.8.8.8"], min_interval=0.0
    ) as fetcher:
        robots = RobotsChecker(fetcher.fetch, "test")
        report = extract_articles(store, fetcher.fetch, robots)
    assert report.extracted == 0
    assert len(store.pending_extraction()) == 3


# ---- robots.txt and Crawl-delay


def test_robots_disallowed_article_is_not_fetched():
    store = store_with("secret")
    robots = RobotsChecker(lambda url: (200, b"User-agent: *\nDisallow: /secret"), "test")
    fetch = guarded_fetch(lambda url: (200, PAGE))
    report = extract_articles(store, fetch, robots)
    assert report.failures == {str(ErrorCode.ROBOTS_DISALLOWED): 1}
    assert fetch.calls == []


def test_robots_are_checked_on_the_host_a_redirect_lands_on():
    store = store_with("a")
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.headers["Host"]
        requested.append((host, request.url.path))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, content=b"User-agent: *\nDisallow: /") if host == "other.example.com" else httpx.Response(404)
        return httpx.Response(301, headers={"Location": "https://other.example.com/page"})

    with HttpFetcher(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["8.8.8.8"], min_interval=0.0
    ) as fetcher:
        report = extract_articles(store, fetcher.fetch, RobotsChecker(fetcher.fetch, "test"))
    assert report.failures == {str(ErrorCode.ROBOTS_DISALLOWED): 1}
    assert ("other.example.com", "/robots.txt") in requested  # its rules were read
    assert ("other.example.com", "/page") not in requested  # and the page was never requested


def test_crawl_delay_from_robots_is_passed_to_the_fetcher():
    store = store_with("a")
    robots = RobotsChecker(lambda url: (200, b"User-agent: *\nCrawl-delay: 5"), "test")
    delays = {}
    extract_articles(
        store, guarded_fetch(lambda url: (200, PAGE)), robots, set_host_delay=delays.__setitem__
    )
    assert delays == {"news.example.com": 5.0}


def test_a_host_asking_for_too_long_a_delay_is_skipped_not_shortened():
    store = store_with("a")
    robots = RobotsChecker(lambda url: (200, b"User-agent: *\nCrawl-delay: 120"), "test")
    delays = {}
    fetch = guarded_fetch(lambda url: (200, PAGE))
    report = extract_articles(store, fetch, robots, set_host_delay=delays.__setitem__)
    assert report.failures == {str(ErrorCode.CRAWL_DELAY_TOO_LONG): 1}
    assert fetch.calls == [] and delays == {}
