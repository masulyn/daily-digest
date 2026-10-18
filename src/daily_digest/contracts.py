"""Shared data contracts (FR-1.1, FR-1.5). Every handoff is validated against these."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    """Error taxonomy shared by all agents (FR-1.5). More codes are added as needed."""

    FEED_UNREACHABLE = "FEED_UNREACHABLE"
    ROBOTS_DISALLOWED = "ROBOTS_DISALLOWED"
    UNSAFE_URL = "UNSAFE_URL"
    FEED_INVALID = "FEED_INVALID"
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
    published: datetime  # timezone-aware, UTC
    extract: str = Field(max_length=1200)
    content_hash: str
