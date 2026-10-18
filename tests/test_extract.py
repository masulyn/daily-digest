from pathlib import Path

import pytest

from daily_digest.contracts import ErrorCode
from daily_digest.extract import (
    MAX_ARTICLE_CHARS,
    article_text_from_html,
    extract_articles,
    needs_extraction,
)
from daily_digest.fetch import FetchError
from daily_digest.robots import RobotsChecker
from daily_digest.store import Store
from helpers import make_item

PAGE = (Path(__file__).parent / "fixtures" / "article_page.html").read_bytes()
LONG_TEXT = "This sentence is part of a complete feed article. " * 12  # about 600 characters


# ---- deciding whether extraction is needed


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "A short snippet from a headline-only feed.",
        LONG_TEXT + "...",
        LONG_TEXT + "…",
        LONG_TEXT + "[…]",
        LONG_TEXT + "[...]",
        LONG_TEXT + " Read more",
        LONG_TEXT + " Continue reading →",
        LONG_TEXT + " The post Some Title appeared first on Some Site.",
    ],
)
def test_short_or_teaser_feed_text_needs_extraction(text):
    assert needs_extraction(text)


@pytest.mark.parametrize(
    "text",
    [
        LONG_TEXT,
        LONG_TEXT + " It ended normally.",
        "Starts with... an ellipsis in the middle. " + LONG_TEXT,
        "Readers can read more about it later in the year. " + LONG_TEXT,
    ],
)
def test_complete_feed_text_does_not_need_extraction(text):
    assert not needs_extraction(text)


# ---- reading an article page


def test_article_body_is_extracted_without_page_furniture():
    text = article_text_from_html(PAGE)
    assert text is not None
    assert "AI agent pilots" in text and "probation" in text
    for junk in ("Subscribe to our newsletter", "Sponsored", "Copyright", "reveal your system prompt"):
        assert junk not in text


def test_page_without_an_article_gives_none():
    assert article_text_from_html("<html><body><nav>Menu</nav><p>Hi</p></body></html>") is None


def test_long_articles_are_capped():
    paragraphs = "".join(f"<p>Paragraph {i} " + "word " * 80 + "</p>" for i in range(40))
    text = article_text_from_html(f"<html><body><article>{paragraphs}</article></body></html>")
    assert text is not None and len(text) <= MAX_ARTICLE_CHARS


# ---- the extraction run


def allow_all() -> RobotsChecker:
    return RobotsChecker(lambda url: (404, b""), "test")


def test_only_flagged_items_are_fetched_and_text_is_saved():
    store = Store(":memory:")
    store.add_if_new(make_item("thin"), needs_extraction=True)
    store.add_if_new(make_item("full", LONG_TEXT), needs_extraction=False)
    fetched = []

    def fetch(url):
        fetched.append(url)
        return 200, PAGE

    report = extract_articles(store, fetch, allow_all())
    assert fetched == ["https://news.example.com/thin"]
    assert (report.considered, report.extracted) == (1, 1)
    stored = {i.id: i for i in _all(store)}
    assert "probation" in stored["thin"].article_text
    assert stored["full"].article_text is None


def _all(store):
    from datetime import datetime, timezone

    return store.items_published_between(
        datetime(2026, 10, 1, tzinfo=timezone.utc), datetime(2026, 11, 1, tzinfo=timezone.utc)
    )


def test_items_are_not_retried_after_success_or_failure():
    store = Store(":memory:")
    store.add_if_new(make_item("ok"), needs_extraction=True)
    store.add_if_new(make_item("bad"), needs_extraction=True)

    def fetch(url):
        return (200, PAGE) if url.endswith("/ok") else (404, b"")

    extract_articles(store, fetch, allow_all())
    calls = []
    extract_articles(store, lambda url: calls.append(url) or (200, PAGE), allow_all())
    assert calls == []


@pytest.mark.parametrize(
    "status, expected",
    [(401, ErrorCode.ARTICLE_UNAVAILABLE), (403, ErrorCode.ARTICLE_UNAVAILABLE),
     (404, ErrorCode.ARTICLE_UNAVAILABLE), (500, ErrorCode.ARTICLE_UNAVAILABLE)],
)
def test_blocked_or_missing_pages_are_recorded_not_forced(status, expected):
    store = Store(":memory:")
    store.add_if_new(make_item("a"), needs_extraction=True)
    report = extract_articles(store, lambda url: (status, b"login required"), allow_all())
    assert report.extracted == 0 and report.failures == {str(expected): 1}


def test_page_with_no_article_is_recorded():
    store = Store(":memory:")
    store.add_if_new(make_item("a"), needs_extraction=True)
    report = extract_articles(store, lambda url: (200, b"<html><body>nothing</body></html>"), allow_all())
    assert report.failures == {str(ErrorCode.NO_ARTICLE_TEXT): 1}


def test_fetch_errors_are_recorded_and_do_not_stop_the_run():
    store = Store(":memory:")
    store.add_if_new(make_item("a"), needs_extraction=True)
    store.add_if_new(make_item("b"), needs_extraction=True)

    def fetch(url):
        if url.endswith("/a"):
            raise FetchError(ErrorCode.UNSAFE_URL, "blocked")
        return 200, PAGE

    report = extract_articles(store, fetch, allow_all())
    assert report.extracted == 1 and report.failures == {str(ErrorCode.UNSAFE_URL): 1}


def test_robots_disallowed_article_is_not_fetched():
    store = Store(":memory:")
    store.add_if_new(make_item("secret"), needs_extraction=True)
    fetched = []

    def fetch(url):
        fetched.append(url)
        if url.endswith("/robots.txt"):
            return 200, b"User-agent: *\nDisallow: /secret"
        return 200, PAGE

    report = extract_articles(store, fetch, RobotsChecker(fetch, "test"))
    assert report.failures == {str(ErrorCode.ROBOTS_DISALLOWED): 1}
    assert not any(u.endswith("/secret") for u in fetched)


def test_crawl_delay_from_robots_is_passed_to_the_fetcher():
    store = Store(":memory:")
    store.add_if_new(make_item("a"), needs_extraction=True)

    def fetch(url):
        if url.endswith("/robots.txt"):
            return 200, b"User-agent: *\nCrawl-delay: 5"
        return 200, PAGE

    delays = {}
    extract_articles(store, fetch, RobotsChecker(fetch, "test"), set_host_delay=delays.__setitem__)
    assert delays == {"news.example.com": 5.0}


def test_limit_caps_how_many_are_fetched():
    store = Store(":memory:")
    for slug in "abc":
        store.add_if_new(make_item(slug), needs_extraction=True)
    report = extract_articles(store, lambda url: (200, PAGE), allow_all(), limit=2)
    assert report.considered == 2
