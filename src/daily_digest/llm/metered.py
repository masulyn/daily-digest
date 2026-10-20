"""Budget checks, retries, cost accounting and the per-call debug log around any client."""

import json
import logging
import math
import time
from collections.abc import Callable
from decimal import Decimal

from pydantic import BaseModel

from ..config import LLMSettings, ModelPrices
from .base import (
    BudgetExceeded,
    LLMClient,
    LLMResult,
    OutputInvalidError,
    PermanentLLMError,
    T,
    TransientLLMError,
    Usage,
)

log = logging.getLogger("daily_digest.llm")

MAX_ATTEMPTS = 3  # the first try plus two retries
_BACKOFF_SECONDS = (1.0, 2.0)
_CHARS_PER_TOKEN = 2.7  # measured about 2.8 on real digests (Oct 2026), so the estimate errs a little high
_MILLION = Decimal(1_000_000)


def estimate_input_tokens(system: str, messages: list[dict], schema: type[BaseModel] | None = None) -> int:
    """A cheap estimate that errs a little high; the exact count is only known from the reply."""
    chars = len(system) + sum(len(str(m["content"])) for m in messages)
    if schema is not None:
        chars += len(json.dumps(schema.model_json_schema()))  # the output format is sent as input too
    return math.ceil(chars / _CHARS_PER_TOKEN)


def cost_usd(prices: ModelPrices, input_tokens: int, output_tokens: int) -> Decimal:
    return (input_tokens * prices.input_per_mtok + output_tokens * prices.output_per_mtok) / _MILLION


def format_usd(amount: Decimal) -> str:
    return f"${amount:.4f}"


class MeteredClient:
    """Wraps a client. Retries count toward cost, and no call is made that could cross a cap."""

    def __init__(
        self,
        inner: LLMClient,
        settings: LLMSettings,
        *,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.max_output_tokens = settings.max_output_tokens
        self._inner = inner
        self._settings = settings
        self._prices = settings.prices[settings.model]
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.spent = Decimal(0)

    def usage_line(self) -> str:
        return (
            f"usage: {self.calls} call(s), {self.input_tokens:,} tokens in, "
            f"{self.output_tokens:,} out, {format_usd(self.spent)}"
        )

    def complete(
        self, *, system: str, messages: list[dict], schema: type[T], max_tokens: int
    ) -> LLMResult[T]:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._check_budget(system, messages, schema, max_tokens, attempt)
            try:
                result = self._inner.complete(
                    system=system, messages=messages, schema=schema, max_tokens=max_tokens
                )
            except TransientLLMError as error:
                self._record(error.usage, attempt, "transient_error")
                if attempt == MAX_ATTEMPTS:
                    raise
                self._sleep(_BACKOFF_SECONDS[attempt - 1])
                continue
            except OutputInvalidError as error:
                self._record(error.usage, attempt, "invalid_output")
                raise
            except PermanentLLMError as error:
                self._record(error.usage, attempt, "permanent_error")
                raise
            self._record(result.usage, attempt, "ok")
            return result
        raise AssertionError("unreachable")

    def _check_budget(
        self, system: str, messages: list[dict], schema: type[BaseModel], max_tokens: int, attempt: int
    ) -> None:
        s = self._settings
        estimated_in = estimate_input_tokens(system, messages, schema)
        worst = self.spent + cost_usd(self._prices, estimated_in, max_tokens)
        problem = None
        if estimated_in > s.max_input_tokens:
            problem = f"about {estimated_in:,} input tokens exceeds the {s.max_input_tokens:,} ceiling"
        elif max_tokens > s.max_output_tokens:
            problem = f"{max_tokens:,} output tokens exceeds the {s.max_output_tokens:,} ceiling"
        elif worst > s.max_usd_per_run:
            problem = (
                f"estimated worst case {format_usd(worst)} would exceed the "
                f"{format_usd(s.max_usd_per_run)} run cap"
            )
        if problem:
            self._log(attempt, "blocked_by_budget", Usage(), Decimal(0))
            raise BudgetExceeded(f"budget exceeded, no model call made: {problem}")

    def _record(self, usage: Usage, attempt: int, outcome: str) -> None:
        call_cost = cost_usd(self._prices, usage.input_tokens, usage.output_tokens)
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.spent += call_cost
        self._log(attempt, outcome, usage, call_cost)

    def _log(self, attempt: int, outcome: str, usage: Usage, call_cost: Decimal) -> None:
        # Numbers and labels only: never request or response bodies, headers, or the key.
        log.debug(
            "llm call model=%s attempt=%d outcome=%s input_tokens=%d output_tokens=%d "
            "call_usd=%s cumulative_usd=%s remaining_usd=%s",
            self._settings.model, attempt, outcome, usage.input_tokens, usage.output_tokens,
            format_usd(call_cost), format_usd(self.spent),
            format_usd(self._settings.max_usd_per_run - self.spent),
        )
