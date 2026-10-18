"""Read RSS/Atom feeds into normalized, de-duplicated items (FR-3.1 to FR-3.3, FR-3.7).

One failing feed never stops the others. Items without a link, title or publish date
are skipped and counted. Fetching is passed in as a function so tests need no network.
"""

import calendar
from dataclasses import dataclass
from datetime import datetime, timezone

import feedparser

from .contracts import CandidateItem, ErrorCode, Source
from .fetch import FetchError
from .normalize import (
    canonicalize_url,
    content_hash,
    item_id,
    make_extract,
    strip_html,
)
from .robots import FetchFn, RobotsChecker
from .store import Store


@dataclass
class FeedReport:
    source: str
    entries: int = 0
    stored: int = 0
    duplicates: int = 0
    skipped: int = 0  # missing link, title or publish date
    error: ErrorCode | None = None


def _published(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def _body_text(entry) -> str:
    content = entry.get("content")
    html = content[0].get("value", "") if content else entry.get("summary", "")
    return strip_html(html)


def entry_to_item(source_name: str, entry) -> CandidateItem | None:
    link = (entry.get("link") or "").strip()
    title = strip_html(entry.get("title", ""))
    published = _published(entry)
    if not link or not title or published is None:
        return None
    url = canonicalize_url(link)
    extract = make_extract(_body_text(entry))
    return CandidateItem(
        id=item_id(url),
        source=source_name,
        canonical_url=url,
        title=title,
        published=published,
        extract=extract,
        content_hash=content_hash(title, extract),
    )


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
        for entry in feed.entries:
            report.entries += 1
            item = entry_to_item(source.name, entry)
            if item is None:
                report.skipped += 1
            elif store.add_if_new(item):
                report.stored += 1
            else:
                report.duplicates += 1
    return reports
