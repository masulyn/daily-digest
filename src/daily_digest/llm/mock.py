"""A canned-response client for tests. The nth call returns the nth response."""

from pydantic import BaseModel, ValidationError

from .base import LLMResult, OutputInvalidError, T, Usage, describe_validation_error


class MockClient:
    def __init__(
        self,
        responses: list[str | BaseModel | Exception],
        *,
        usage: Usage = Usage(input_tokens=1000, output_tokens=200),
        max_input_tokens: int = 40_000,
        max_output_tokens: int = 4000,
    ) -> None:
        self._responses = list(responses)
        self._usage = usage
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.requests: list[dict] = []

    def complete(
        self, *, system: str, messages: list[dict], schema: type[T], max_tokens: int
    ) -> LLMResult[T]:
        self.requests.append({"system": system, "messages": messages, "max_tokens": max_tokens})
        assert self._responses, "mock client has no canned response left"
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        text = response if isinstance(response, str) else response.model_dump_json()
        try:
            return LLMResult(schema.model_validate_json(text), self._usage)
        except ValidationError as error:
            raise OutputInvalidError(describe_validation_error(error), self._usage, text) from None
