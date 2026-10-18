"""Small builders shared by tests."""

from datetime import datetime, timezone

from daily_digest.contracts import CandidateItem


def make_item(slug: str, extract: str = "") -> CandidateItem:
    return CandidateItem(
        id=slug, source="s", canonical_url=f"https://news.example.com/{slug}", title=f"Story {slug}",
        published=datetime(2026, 10, 14, 12, tzinfo=timezone.utc), extract=extract,
        content_hash=f"hash-{slug}",
    )
