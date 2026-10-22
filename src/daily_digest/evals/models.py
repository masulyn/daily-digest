"""Contracts for the eval suite: cases, judge verdicts, and recorded results."""

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from ..contracts import CandidateItem, IssueCategory, IssueSection

DIMENSIONS = ("faithfulness", "coverage", "audience_fit", "concision")
CASE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class Case(BaseModel):
    """One saved day. `items` are full records; the runner loads them into a temporary store."""

    model_config = ConfigDict(extra="forbid")

    id: str
    category: str = Field(min_length=1)
    description: str = Field(min_length=1)
    target_date: date
    items: list[CandidateItem]
    must_cover: list[str] = Field(default_factory=list)  # item ids a good digest draws on
    canaries: list[str] = Field(default_factory=list)  # strings that must not appear in the digest

    @field_validator("id")
    @classmethod
    def _id_is_a_safe_file_name(cls, value: str) -> str:
        if not CASE_ID.match(value):
            raise ValueError("must be lowercase letters, digits, - or _ (it names a folder)")
        return value

    @field_validator("canaries")
    @classmethod
    def _canaries_are_not_blank(cls, value: list[str]) -> list[str]:
        if any(len(c.strip()) < 6 for c in value):
            raise ValueError("each canary must be at least 6 characters, or it would match ordinary text")
        return value

    @field_validator("items")
    @classmethod
    def _item_ids_are_unique(cls, value: list[CandidateItem]) -> list[CandidateItem]:
        ids = [item.id for item in value]
        if len(set(ids)) != len(ids):
            raise ValueError("item ids must be unique")
        return value

    @field_validator("must_cover")
    @classmethod
    def _must_cover_names_items(cls, value: list[str], info: ValidationInfo) -> list[str]:
        known = {item.id for item in info.data.get("items", [])}
        if any(i not in known for i in value):
            raise ValueError("every id must be an item of this case")
        return value


class JudgeScore(BaseModel):
    score: int = Field(ge=1, le=5)
    rationale: str = Field(min_length=1)


class UnsupportedSentence(BaseModel):
    section: IssueSection
    index: int | None = None  # absent for the headline
    category: IssueCategory
    problem: str = Field(min_length=1)


class CoversMust(BaseModel):
    item_id: str
    covered: bool


class JudgeVerdict(BaseModel):
    """What the judge model returns."""

    faithfulness: JudgeScore
    coverage: JudgeScore
    audience_fit: JudgeScore
    concision: JudgeScore
    unsupported_sentences: list[UnsupportedSentence] = Field(default_factory=list)
    covers_must: list[CoversMust] = Field(default_factory=list)


class JudgeResult(BaseModel):
    """The verdict after code checks, as recorded."""

    scores: dict[str, int]
    rationales: dict[str, str]
    unsupported_sentences: list[UnsupportedSentence]
    covers_must: dict[str, bool]
    notes: list[str]
    model: str
    usd: str


CaseStatus = Literal["approved", "not_converged", "degraded", "unreviewed", "failed", "no_digest", "error"]
JUDGED_STATUSES = ("approved", "not_converged", "degraded")
BAD_STATUSES = ("failed", "unreviewed", "error")


class CaseResult(BaseModel):
    id: str
    category: str
    status: CaseStatus
    review_rounds: int = 0
    sentences_removed: int = 0
    dropped_sentence_notes: int = 0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: str = "0"  # generator calls
    judge_usd: str = "0"
    judge: JudgeResult | None = None
    judge_error: str | None = None
    error: str | None = None
    failure: str | None = None  # why the case did not pass, as the draft command prints it
    canary_hits: list[str] = Field(default_factory=list)
    words: int | None = None

    @property
    def total_usd(self) -> float:
        return float(self.usd) + float(self.judge_usd)

    @property
    def mean_score(self) -> float | None:
        return sum(self.judge.scores.values()) / len(self.judge.scores) if self.judge else None


class RunConfig(BaseModel):
    generator_model: str
    judge_model: str
    prices: dict[str, dict[str, str]]
    settings: dict[str, str]
    max_usd_per_eval: str
    mock: bool
    judge_enabled: bool
    git_dirty: bool


class RunResults(BaseModel):
    run_id: str
    started_at: datetime
    config: RunConfig
    prompt_hashes: dict[str, str]
    rubric_version: str
    cases: list[CaseResult]
    skipped_cases: list[str] = Field(default_factory=list)  # not started because of the spend cap
    stopped_by_cap: bool = False
    total_usd: str = "0"


class BaselineCase(BaseModel):
    id: str
    status: CaseStatus
    scores: dict[str, int] | None = None
    canary_hits: list[str] = Field(default_factory=list)


class Baseline(BaseModel):
    run_id: str
    config: RunConfig
    prompt_hashes: dict[str, str]
    rubric_version: str
    cases: list[BaselineCase]
