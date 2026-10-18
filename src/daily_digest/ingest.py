"""Read RSS/Atom feeds into normalized, de-duplicated items.

One failing feed never stops the others. Fetching is passed in as a function so tests
need no network.
"""

import calendar
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser
from pydantic import ValidationError

from .contracts import CandidateItem, ErrorCode, Source
from .fetch import FetchError, FetchFn
from .normalize import (
    canonicalize_url,
    content_hash,
    item_id,
    make_extract,
    needs_extraction,
    strip_html,
)
from .robots import RobotsChecker
from .store import Store


@dataclass
class FeedReport:
    source: str
    entries: int = 0
    stored: int = 0
    duplicates: int = 0
    skipped: int = 0  # missing link, title or publish date, or failed validation
    error: ErrorCode | None = None


def _published(entry: feedparser.FeedParserDict) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def _body_text(entry: feedparser.FeedParserDict) -> str:
    content = entry.get("content")
    html = content[0].get("value", "") if content else entry.get("summary", "")
    return strip_html(html)


def parse_entry(
    source_name: str, entry: feedparser.FeedParserDict
) -> tuple[CandidateItem, bool] | None:
    """The item, and whether its article should be fetched later; None if unusable."""
    link = (entry.get("link") or "").strip()
    title = strip_html(entry.get("title", ""))
    published = _published(entry)
    if not link or not title or published is None:
        return None
    url = canonicalize_url(link)
    body = _body_text(entry)
    extract = make_extract(body)
    # Headline-only feeds have no extract; hashing the title alone would merge
    # unrelated posts that share a title (such as a recurring "Weekly roundup").
    try:
        item = CandidateItem(
            id=item_id(url),
            source=source_name,
            canonical_url=url,
            title=title,
            published=published,
            extract=extract,
            content_hash=content_hash(title, extract or url),
        )
    except ValidationError:
        return None
    return item, needs_extraction(body)


def ingest(sources: list[Source], fetch: FetchFn, robots: RobotsChecker, store: Store) -> list[FeedReport]:
    reports: list[FeedReport] = []
    for source in sources:
        report = FeedReport(source=source.name)
        reports.append(report)
        if not robots.allowed(source.url):
            report.error = ErrorCode.ROBOTS_DISALLOWED
            continue
        try:
            status, body = fetch(source.url)
        except FetchError as e:
            report.error = e.code
            continue
        if status != 200:
            report.error = ErrorCode.FEED_UNREACHABLE
            continue
        feed = feedparser.parse(body)
        if feed.bozo and not feed.entries:
            report.error = ErrorCode.FEED_INVALID
            continue
        with store.transaction():
            for entry in feed.entries:
                report.entries += 1
                parsed = parse_entry(source.name, entry)
                if parsed is None:
                    report.skipped += 1
                elif store.add_if_new(parsed[0], needs_extraction=parsed[1]):
                    report.stored += 1
                else:
                    report.duplicates += 1
    return reports
