"""The model interface and its errors. Agents depend on this, never on a provider SDK."""

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class Usage(BaseModel, frozen=True):
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class LLMResult(Generic[T]):
    parsed: T
    usage: Usage


class LLMError(Exception):
    """Base for model-call failures. `usage` is what the failed attempt is known to have cost."""

    def __init__(self, message: str, usage: Usage = Usage()) -> None:
        super().__init__(message)
        self.usage = usage


class TransientLLMError(LLMError):
    """Worth retrying: rate limit, server error, timeout, connection failure."""


class PermanentLLMError(LLMError):
    """Retrying cannot help: bad key, bad request, refusal, truncated output."""


class OutputInvalidError(LLMError):
    """The reply did not match the output schema. `raw_text` is what the model said."""

    def __init__(self, message: str, usage: Usage = Usage(), raw_text: str = "") -> None:
        super().__init__(message, usage)
        self.raw_text = raw_text


class BudgetExceeded(LLMError):
    """Raised before a call that could cross a cap. Nothing was sent."""


class LLMClient(Protocol):
    max_output_tokens: int

    def complete(
        self, *, system: str, messages: list[dict], schema: type[T], max_tokens: int
    ) -> LLMResult[T]:
        """One model call whose reply is validated against `schema`."""
