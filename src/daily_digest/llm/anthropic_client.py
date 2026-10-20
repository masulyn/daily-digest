"""Anthropic Messages API adapter. The only module that imports the Anthropic SDK."""

import anthropic
import httpx2
from pydantic import SecretStr, ValidationError

from .base import (
    LLMResult,
    OutputInvalidError,
    PermanentLLMError,
    T,
    TransientLLMError,
    Usage,
    describe_validation_error,
)

# Pinned in code so an environment variable cannot send the key to another host.
API_BASE_URL = "https://api.anthropic.com"
_TRANSIENT_STATUSES = {408, 409, 429}
# Docs don't say which errors are billed; 408/504 may have been, the other statuses are assumed not.
_USAGE_UNKNOWN_STATUSES = {408, 504}
# The request never left the machine, so it cannot have been billed.
_NEVER_SENT = (httpx2.ConnectError, httpx2.ConnectTimeout)


class AnthropicClient:
    """One request per `complete` call. Retries and cost tracking live in `MeteredClient`."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        timeout_seconds: float,
        http_client=None,
    ) -> None:
        self._model = model
        # max_retries=0: the SDK would retry silently and hide attempts from the cost log.
        self._client = anthropic.Anthropic(
            api_key=api_key.get_secret_value(),
            base_url=API_BASE_URL,
            max_retries=0,
            timeout=timeout_seconds,
            http_client=http_client,
        )

    def complete(
        self, *, system: str, messages: list[dict], schema: type[T], max_tokens: int
    ) -> LLMResult[T]:
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                # Low effort keeps hidden reasoning from eating the output-token cap.
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)},
                },
            )
        except anthropic.APITimeoutError as error:
            # The SDK raises these `from` the httpx2 error, which says whether the request was sent.
            raise TransientLLMError(
                "the model request timed out", usage_known=isinstance(error.__cause__, _NEVER_SENT)
            ) from None
        except anthropic.APIConnectionError as error:
            raise TransientLLMError(
                "could not connect to the model API", usage_known=isinstance(error.__cause__, _NEVER_SENT)
            ) from None
        except anthropic.APIStatusError as error:
            # Built from the status only: error bodies and request headers stay out of messages.
            message = f"the model API returned HTTP {error.status_code}"
            if error.status_code in _TRANSIENT_STATUSES or error.status_code >= 500:
                raise TransientLLMError(
                    message, usage_known=error.status_code not in _USAGE_UNKNOWN_STATUSES
                ) from None
            raise PermanentLLMError(message) from None

        usage = Usage(
            input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens
        )
        if response.stop_reason == "refusal":
            raise PermanentLLMError("the model declined the request", usage)
        if response.stop_reason == "max_tokens":
            raise PermanentLLMError("the model reply was cut off at the output-token limit", usage)
        text = next((block.text for block in response.content if block.type == "text"), "")
        try:
            return LLMResult(schema.model_validate_json(text), usage)
        except ValidationError as error:
            raise OutputInvalidError(describe_validation_error(error), usage, text) from None
