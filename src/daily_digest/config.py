"""Load the feed list and resolve where local data lives."""

import os
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .contracts import Source

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FEEDS_PATH = PROJECT_ROOT / "config" / "feeds.toml"
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.toml"
DEFAULT_NEWSLETTERS_DIR = PROJECT_ROOT / "newsletters"


class _FeedsFile(BaseModel):
    feeds: list[Source]


def load_sources(path: Path = DEFAULT_FEEDS_PATH) -> list[Source]:
    """Return enabled sources. A bad file raises a validation error naming the key."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    parsed = _FeedsFile.model_validate(data)
    return [s for s in parsed.feeds if s.enabled]


def data_dir() -> Path:
    """Where the database lives. Override with DIGEST_DATA_DIR."""
    return Path(os.environ.get("DIGEST_DATA_DIR", PROJECT_ROOT / "data"))


class ModelPrices(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_per_mtok: Decimal = Field(gt=0)
    output_per_mtok: Decimal = Field(gt=0)


class LLMSettings(BaseModel):
    """Model and budget settings. Every key is required so a gap fails at startup."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["anthropic", "mock"]
    model: str = Field(min_length=1)
    prices: dict[str, ModelPrices]
    max_usd_per_run: Decimal = Field(gt=0)
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_run_input_tokens: int = Field(gt=0)
    max_run_output_tokens: int = Field(gt=0)
    max_review_rounds: int = Field(ge=1)
    request_timeout_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _model_has_a_price(self) -> "LLMSettings":
        if self.model not in self.prices:
            raise ValueError(f"no entry for model {self.model!r} in llm.prices")
        return self

    @model_validator(mode="after")
    def _run_ceilings_allow_a_call(self) -> "LLMSettings":
        # A run ceiling below the per-call one would block a call the per-call check allows.
        for run_key, call_key in (
            ("max_run_input_tokens", "max_input_tokens"),
            ("max_run_output_tokens", "max_output_tokens"),
        ):
            if getattr(self, run_key) < getattr(self, call_key):
                raise ValueError(f"llm.{run_key} must be at least llm.{call_key}")
        return self


class EvalSettings(BaseModel):
    """The eval suite's judge and spending limits."""

    model_config = ConfigDict(extra="forbid")

    judge_model: str = Field(min_length=1)
    max_usd_per_eval: Decimal = Field(gt=0)  # generator and judge calls together
    max_usd_per_judge: Decimal = Field(gt=0)  # one judged case


class SettingsError(Exception):
    """The settings file is missing or has a bad value; the message names the key."""


class _SettingsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: LLMSettings
    eval: EvalSettings | None = None


def load_llm_settings(path: Path = DEFAULT_SETTINGS_PATH) -> LLMSettings:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise SettingsError(f"settings file not found: {path}") from None
    except tomllib.TOMLDecodeError as error:
        raise SettingsError(f"settings file {path} is not valid TOML: {error}") from None
    try:
        return _SettingsFile.model_validate(data).llm
    except ValidationError as error:
        first = error.errors()[0]
        key = ".".join(str(part) for part in first["loc"]) or "llm"
        raise SettingsError(f"invalid setting {key}: {first['msg']}") from None


def load_eval_settings(path: Path = DEFAULT_SETTINGS_PATH) -> EvalSettings:
    """The `[eval]` table. The judge model needs a price entry like any other model."""
    load_llm_settings(path)  # reports a missing or invalid file the same way for every command
    with open(path, "rb") as f:
        data = tomllib.load(f)
    try:
        parsed = _SettingsFile.model_validate(data)
    except ValidationError as error:
        first = error.errors()[0]
        key = ".".join(str(part) for part in first["loc"]) or "eval"
        raise SettingsError(f"invalid setting {key}: {first['msg']}") from None
    if parsed.eval is None:
        raise SettingsError("invalid setting eval: the [eval] table is missing")
    if parsed.eval.judge_model not in parsed.llm.prices:
        raise SettingsError(f"invalid setting eval.judge_model: no entry for model {parsed.eval.judge_model!r} in llm.prices")
    return parsed.eval
