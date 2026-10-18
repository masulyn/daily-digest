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
    content_hash: str
