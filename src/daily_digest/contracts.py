"""Shared data contracts. Every handoff between components is validated against these."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, BeforeValidator, Field, field_validator

from .domains import normalize_domain


class ErrorCode(StrEnum):
    FEED_UNREACHABLE = "FEED_UNREACHABLE"
    ROBOTS_DISALLOWED = "ROBOTS_DISALLOWED"
    UNSAFE_URL = "UNSAFE_URL"
    DOMAIN_NOT_ALLOWED = "DOMAIN_NOT_ALLOWED"  # a link points to a host the feed is not allowed to link to
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
    # Hosts this feed's links may point to, with their subdomains. Absent: the feed URL's own domain.
    allowed_domains: list[str] | None = None

    @field_validator("allowed_domains")
    @classmethod
    def _domains_are_bare_domains(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = []
        for number, entry in enumerate(value, start=1):
            try:
                cleaned.append(normalize_domain(entry))
            except ValueError as error:
                raise ValueError(f"entry {number}: {error}") from None  # the entry itself is not echoed
        return cleaned


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


SentenceType = Literal["factual", "quote", "attributed_opinion", "trend", "transition"]
SectionName = Literal["what_you_need_to_know", "worth_a_closer_look"]
# One flat enum: a union with a one-value Literal is sent to the API as an unconstrained string.
IssueSection = Literal["what_you_need_to_know", "worth_a_closer_look", "headline"]


class DraftSentence(BaseModel):
    """One sentence of the digest and what supports it (the provenance map, FR-1.1)."""

    text: str = Field(min_length=1)
    type: SentenceType
    item_ids: list[str] = Field(default_factory=list)
    quote: str | None = None  # the exact quoted string; only for type "quote"
    speaker: str | None = None  # for "quote" and "attributed_opinion"
    starts_paragraph: bool = False


class Draft(BaseModel):
    headline: str = Field(min_length=1)
    what_you_need_to_know: list[DraftSentence]
    worth_a_closer_look: list[DraftSentence]


class DraftIssue(BaseModel):
    """A required change to one sentence (or to the headline when `index` is None)."""

    section: IssueSection
    index: int | None = None
    required_change: str = Field(min_length=1)


Severity = Literal["blocking", "advisory"]
IssueCategory = Literal[
    "unsupported", "overstated", "opinion", "promotional", "hedge_lost",
    "attribution", "disagreement", "tone", "other",
]
ReviewStatus = Literal["approved", "not_converged", "degraded", "unreviewed", "failed"]


class ReviewIssue(BaseModel):
    """One problem with one sentence (or the headline). It asks for a reword or a removal, never an addition."""

    section: IssueSection
    index: int | None = None  # sentence position within the section, counted from 0; absent for the headline
    severity: Severity
    category: IssueCategory
    required_change: Annotated[str, BeforeValidator(lambda v: v.strip()[:300] if isinstance(v, str) else v)] = Field(
        min_length=1, max_length=300
    )  # the API cannot enforce a length, so an over-long reply is clipped rather than failing it


class ReviewResult(BaseModel):
    issues: list[ReviewIssue] = Field(default_factory=list)
    # The model's own verdict is never trusted; code recomputes it from the issues.
    verdict: Literal["approve", "revise"] = "revise"


class ReviewRound(BaseModel):
    """One model review: the draft that was judged and every issue found (mechanical and model)."""

    number: int = Field(ge=1)
    draft: Draft
    issues: list[ReviewIssue]


class ReviewSummary(BaseModel):
    status: ReviewStatus
    rounds: int = Field(default=0, ge=0)  # model reviews that ran
    removed: int = Field(default=0, ge=0)  # sentences code removed because they stayed blocked
    unresolved: list[str] = Field(default_factory=list)  # blocking issues left, as text
    advisory: list[str] = Field(default_factory=list)


class CitedSource(BaseModel):
    number: int = Field(ge=1)
    title: str
    source: str
    published: AwareDatetime
    url: str


class FinalDigest(BaseModel):
    covered_date: date
    generated_at: AwareDatetime
    headline: str
    what_you_need_to_know: list[str]  # paragraphs with [n] markers
    worth_a_closer_look: list[str]
    sources: list[CitedSource]
    word_count: int
    reading_time_minutes: int
    notes: list[str] = Field(default_factory=list)
    review: ReviewSummary = Field(default_factory=lambda: ReviewSummary(status="unreviewed"))


class ProvenanceEntry(BaseModel):
    """What supports one sentence of the saved digest (FR-1.1). The sentence text is in the paragraphs."""

    section: SectionName
    index: int = Field(ge=0)
    type: SentenceType
    item_ids: list[str]
    quote: str | None = None
    speaker: str | None = None


class UsageRecord(BaseModel):
    model: str
    calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    usd: Decimal
    unknown_usage_attempts: int = Field(default=0, ge=0)


class PublishedDigest(BaseModel):
    """The JSON saved next to the Markdown: the digest, its provenance map, and what the run cost."""

    digest: FinalDigest
    provenance: list[ProvenanceEntry]
    usage: UsageRecord
