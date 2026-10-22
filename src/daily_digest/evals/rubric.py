"""Load the versioned rubric."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from .models import DIMENSIONS


class Dimension(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str
    anchor_1: str
    anchor_3: str
    anchor_5: str


class Tiers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    high: str
    medium: str
    exclude: str


class Rubric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    audience: str
    tiers: Tiers
    dimensions: dict[str, Dimension]


class RubricError(Exception):
    """The rubric file is missing or wrong; the message names the key."""


def load_rubric(path: Path) -> Rubric:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        rubric = Rubric.model_validate(data)
    except FileNotFoundError:
        raise RubricError(f"rubric not found: {path}") from None
    except tomllib.TOMLDecodeError:
        raise RubricError(f"rubric {path.name} is not valid TOML") from None
    except ValidationError as error:
        first = error.errors(include_input=False)[0]
        raise RubricError(f"rubric {path.name}: {'.'.join(str(p) for p in first['loc'])}: {first['msg']}") from None
    if set(rubric.dimensions) != set(DIMENSIONS):
        raise RubricError(f"rubric {path.name}: dimensions must be exactly {', '.join(DIMENSIONS)}")
    return rubric
