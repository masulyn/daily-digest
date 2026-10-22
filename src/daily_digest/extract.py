"""Fetch full article text for items whose feed text is too thin to use.

Which items qualify is decided at ingest by `normalize.needs_extraction`. Pages are
untrusted: only plain text, length-capped, is kept.

Policy: robots.txt is checked on every redirect hop (the fetcher calls our check), a
site's Crawl-delay is honored or that host is skipped, and blocked or login-protected
pages are never forced. Permanent failures are recorded once. Temporary ones (rate limits,
server errors, timeouts) are retried on later runs, up to MAX_EXTRACTION_ATTEMPTS.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from .contracts import ErrorCode, Source
from .domains import allowed_by_source, host_allowed, safe_host, safe_text
from .fetch import BackoffError, FetchError, GuardedFetchFn
from .normalize import make_extract
from .robots import RobotsChecker
from .store import Store

log = logging.getLogger(__name__)

MIN_ARTICLE_CHARS = 200  # less than this is a menu or error page, not an article
MAX_ARTICLE_CHARS = 3000
# Above this we skip the host instead of quietly waiting less than it asked.
MAX_CRAWL_DELAY_SECONDS = 30.0


# A line that doesn't end like a sentence and is this short is a heading or byline, not prose.
_MAX_BYLINE_CHARS = 150
_SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*$")
_EDITORS_NOTE = re.compile(r"editor['’]s note\b", re.I)


def _drop_leading_byline(text: str) -> str:
    """Remove author/title lines and editor's notes that precede the first real paragraph.

    Works on trafilatura's line structure, which the whitespace collapse would erase.
    Only the leading run is touched, so body text is never altered.
    Limits: a short lead-in ending in a colon or no punctuation is dropped; a byline ending
    in a period, or longer than 150 chars, slips through.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    start = 0
    while start < len(lines):
        line = lines[start]
        if _EDITORS_NOTE.match(line) or (
            len(line) <= _MAX_BYLINE_CHARS and not _SENTENCE_END.search(line)
        ):
            start += 1
        else:
            break
    # If nothing looks like prose, keep everything rather than return an empty article.
    return "\n".join(lines[start:] if start < len(lines) else lines)


def article_text_from_html(html: bytes | str) -> str | None:
    """The article body as plain text, or None if no real article was found."""
    import trafilatura  # imported here so commands that never extract don't pay for it

    text = trafilatura.extract(
        html, include_comments=False, include_tables=False, favor_precision=True
    )
    if not text:
        return None
    text = re.sub(r"\s+", " ", _drop_leading_byline(text)).strip()
    if len(text) < MIN_ARTICLE_CHARS:
        return None
    return make_extract(text, MAX_ARTICLE_CHARS)


@dataclass
class ExtractionReport:
    considered: int = 0
    extracted: int = 0
    deferred: int = 0  # skipped because a site asked us to back off; not counted as a try
    failures: dict[str, int] = field(default_factory=dict)


def _is_temporary(status: int) -> bool:
    return status == 429 or status >= 500


def _guard(
    robots: RobotsChecker, set_host_delay: Callable[[str, float], None] | None
) -> Callable[[str], bool]:
    """The per-hop check: robots.txt rules, then the site's Crawl-delay."""

    def allow(url: str) -> bool:
        if not robots.allowed(url):
            return False
        delay = robots.crawl_delay(url)
        if delay:
            if delay > MAX_CRAWL_DELAY_SECONDS:
                raise FetchError(ErrorCode.CRAWL_DELAY_TOO_LONG, f"crawl-delay {delay}s for {url}")
            if set_host_delay:
                set_host_delay(urlsplit(url).hostname or "", delay)
        return True

    return allow


def _record_failure(
    store: Store, report: ExtractionReport, item_id: str, code: ErrorCode, *, final: bool
) -> None:
    store.record_extraction_failure(item_id, str(code), final=final)
    report.failures[str(code)] = report.failures.get(str(code), 0) + 1


def extract_articles(
    store: Store,
    fetch: GuardedFetchFn,
    robots: RobotsChecker,
    *,
    sources: list[Source],
    set_host_delay: Callable[[str, float], None] | None = None,
    limit: int | None = None,
    since: datetime | None = None,
) -> ExtractionReport:
    """Extract text for items flagged at ingest that are still waiting for it.

    A page is fetched only from the feed's allowed domains, and a redirect to any other host ends the
    attempt for good; the item keeps its feed text.
    """
    report = ExtractionReport()
    allow = _guard(robots, set_host_delay)
    allowed = allowed_by_source(sources)
    for item_id, url in store.pending_extraction(limit, since):
        report.considered += 1
        feed = store.source_of(item_id)
        if feed not in allowed:  # the feed was removed from the list; a restored entry makes it fetchable again
            _record_failure(store, report, item_id, ErrorCode.DOMAIN_NOT_ALLOWED, final=False)
            continue
        try:
            status, body = fetch(url, allow=allow, permit=lambda hop, domains=allowed[feed]: host_allowed(hop, domains))
        except BackoffError:
            report.deferred += 1
            continue
        except FetchError as e:
            if e.code == ErrorCode.DOMAIN_NOT_ALLOWED:
                log.warning("%s feed=%s host=%s", e.code, safe_text(feed), e)
                _record_failure(store, report, item_id, e.code, final=True)
                continue
            # Only a connection problem or timeout is worth trying again.
            temporary = e.code == ErrorCode.FEED_UNREACHABLE
            _record_failure(store, report, item_id, e.code, final=not temporary)
            continue
        if status != 200:
            _record_failure(
                store, report, item_id, ErrorCode.ARTICLE_UNAVAILABLE, final=not _is_temporary(status)
            )
            continue
        text = article_text_from_html(body)
        if text is None:
            _record_failure(store, report, item_id, ErrorCode.NO_ARTICLE_TEXT, final=True)
            continue
        store.save_article_text(item_id, text)
        report.extracted += 1
    return report
