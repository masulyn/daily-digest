"""Small builders and fakes shared by tests."""

import json
from datetime import date, datetime, timezone
from pathlib import Path

from daily_digest.contracts import CandidateItem, ErrorCode, Source
from daily_digest.domains import safe_host
from daily_digest.fetch import FetchError


# The feed behind `sample_item`; some tests link to e.com instead.
TEST_SOURCES = [Source(name="s", url="https://news.example.com/feed", category="test", allowed_domains=["example.com", "e.com"])]


def sample_item(slug: str, extract: str = "") -> CandidateItem:
    return CandidateItem(
        id=slug, source="s", canonical_url=f"https://news.example.com/{slug}", title=f"Story {slug}",
        published=datetime(2026, 10, 14, 12, tzinfo=timezone.utc), extract=extract,
        content_hash=f"hash-{slug}",
    )


def guarded_fetch(responder):
    """A fake fetch that keeps the real contract: it asks `allow` before each request.

    `responder(url)` returns (status, body) or an exception to raise. `.calls` lists
    the URLs actually requested.
    """
    calls: list[str] = []

    def fetch(url, allow=None, permit=None):
        if permit is not None and not permit(url):
            raise FetchError(ErrorCode.DOMAIN_NOT_ALLOWED, safe_host(url))
        if allow is not None and not allow(url):
            raise FetchError(ErrorCode.ROBOTS_DISALLOWED, url)
        calls.append(url)
        result = responder(url)
        if isinstance(result, Exception):
            raise result
        return result

    fetch.calls = calls
    return fetch


def editor_items(day: date, *ids: str) -> list[CandidateItem]:
    """Items from the saved Editor fixture, published at local noon on `day`."""
    rows = json.loads((Path(__file__).parent / "fixtures" / "editor_items.json").read_text())
    noon = datetime.combine(day, datetime.min.time().replace(hour=12)).astimezone()
    return [
        CandidateItem(
            canonical_url=f"https://news.example.com/{row['id']}", published=noon,
            content_hash=f"hash-{row['id']}", **row,
        )
        for row in rows
        if not ids or row["id"] in ids
    ]
