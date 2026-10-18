"""Fetch full article text for items whose feed gave too little (FR-3.6).

The app decides by looking at the feed text itself, so no per-feed settings are needed:
- it is short (under MIN_FEED_TEXT_CHARS), or
- it ends like a teaser ("...", "Read more", "appeared first on").
Everything is rule-based and lives in `needs_extraction`, so a new feed with the same
problem is handled without any change.

Rules we follow: robots.txt for the article's own host, the site's Crawl-delay, no
bypassing paywalls or logins (a 401/402/403 is recorded and skipped), and fetched pages
are untrusted: only plain text, length-capped, ever leaves this module.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import trafilatura

from .contracts import ErrorCode
from .fetch import FetchError, FetchFn
from .normalize import make_extract
from .robots import RobotsChecker
from .store import Store

MIN_FEED_TEXT_CHARS = 300
MIN_ARTICLE_CHARS = 200  # less than this is a menu or error page, not an article
MAX_ARTICLE_CHARS = 3000
MAX_CRAWL_DELAY_SECONDS = 30.0

# Endings that mean "the rest is on the website". Add a pattern here to teach the app a new one.
_TEASER_ENDINGS = [
    re.compile(r"(\.\.\.|…|\[\s*(\.\.\.|…)\s*\])$"),
    re.compile(r"\b(read more|continue reading|read the full (article|story)|click to read)\b[^.]{0,40}$", re.I),
    re.compile(r"\bthe post\b.{1,200}\bappeared first on\b.{1,100}$", re.I),
]


def needs_extraction(feed_text: str) -> bool:
    """True if the feed's own text looks too short or cut off to use as the article."""
    text = feed_text.strip()
    return len(text) < MIN_FEED_TEXT_CHARS or any(p.search(text) for p in _TEASER_ENDINGS)


def article_text_from_html(html: bytes | str) -> str | None:
    """The article body as plain text, or None if no real article was found."""
    text = trafilatura.extract(
        html, include_comments=False, include_tables=False, favor_precision=True
    )
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < MIN_ARTICLE_CHARS:
        return None
    return make_extract(text, MAX_ARTICLE_CHARS)


@dataclass
class ExtractionReport:
    considered: int = 0
    extracted: int = 0
    failures: dict[str, int] = field(default_factory=dict)

    def fail(self, code: ErrorCode | str) -> None:
        self.failures[str(code)] = self.failures.get(str(code), 0) + 1


def extract_articles(
    store: Store,
    fetch: FetchFn,
    robots: RobotsChecker,
    *,
    set_host_delay: Callable[[str, float], None] | None = None,
    limit: int | None = None,
) -> ExtractionReport:
    """Extract text for items flagged at ingest that have not been tried yet."""
    report = ExtractionReport()
    for item_id, url in store.pending_extraction(limit):
        report.considered += 1

        def give_up(code: ErrorCode) -> None:
            store.record_extraction_failure(item_id, str(code))
            report.fail(code)

        if not robots.allowed(url):
            give_up(ErrorCode.ROBOTS_DISALLOWED)
            continue
        delay = robots.crawl_delay(url)
        if delay and set_host_delay:
            set_host_delay(urlsplit(url).hostname or "", min(float(delay), MAX_CRAWL_DELAY_SECONDS))
        try:
            status, body = fetch(url)
        except FetchError as e:
            give_up(e.code)
            continue
        if status != 200:
            give_up(ErrorCode.ARTICLE_UNAVAILABLE)  # includes paywall or login responses
            continue
        text = article_text_from_html(body)
        if text is None:
            give_up(ErrorCode.NO_ARTICLE_TEXT)
            continue
        store.save_article_text(item_id, text)
        report.extracted += 1
    return report
