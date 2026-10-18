"""Shared data contracts. Every handoff between components is validated against these."""

from enum import StrEnum

from pydantic import AwareDatetime, BaseModel, Field


class ErrorCode(StrEnum):
    FEED_UNREACHABLE = "FEED_UNREACHABLE"
    ROBOTS_DISALLOWED = "ROBOTS_DISALLOWED"
    UNSAFE_URL = "UNSAFE_URL"
    FEED_INVALID = "FEED_INVALID"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    ARTICLE_UNAVAILABLE = "ARTICLE_UNAVAILABLE"  # page missing, blocked, or behind a login
    NO_ARTICLE_TEXT = "NO_ARTICLE_TEXT"  # page loaded but no article body was found
    CRAWL_DELAY_TOO_LONG = "CRAWL_DELAY_TOO_LONG"  # the site asks for more waiting than we allow


class Source(BaseModel):
    name: str
    url: str
    category: str
    enabled: bool = True


class CandidateItem(BaseModel):
    """One news item, as plain text. Raw HTML never goes past the Researcher."""

    id: str
    source: str
    canonical_url: str
    title: str
    published: AwareDatetime
    extract: str = Field(max_length=1200)
    article_text: str | None = Field(default=None, max_length=3000)  # from extraction, if run
    content_hash: str


class ItemSummary(BaseModel):
    """A short summary of one item, with the URL it came from."""

    item_id: str
    key_points: list[str]
    source_url: str
