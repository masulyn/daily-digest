"""Small builders and fakes shared by tests."""

from datetime import datetime, timezone

from daily_digest.contracts import CandidateItem, ErrorCode
from daily_digest.fetch import FetchError


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

    def fetch(url, allow=None):
        if allow is not None and not allow(url):
            raise FetchError(ErrorCode.ROBOTS_DISALLOWED, url)
        calls.append(url)
        result = responder(url)
        if isinstance(result, Exception):
            raise result
        return result

    fetch.calls = calls
    return fetch
