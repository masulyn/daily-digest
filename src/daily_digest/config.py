"""Load the feed list from config/feeds.toml (FR-1.2, FR-1.3)."""

import tomllib
from pathlib import Path

from pydantic import BaseModel

from .contracts import Source

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FEEDS_PATH = PROJECT_ROOT / "config" / "feeds.toml"


class _FeedsFile(BaseModel):
    feeds: list[Source]


def load_sources(path: Path = DEFAULT_FEEDS_PATH) -> list[Source]:
    """Return enabled sources. A bad file raises a validation error naming the key."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    parsed = _FeedsFile.model_validate(data)
    return [s for s in parsed.feeds if s.enabled]
