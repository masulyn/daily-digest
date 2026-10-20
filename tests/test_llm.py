import ast
import json
import logging
import traceback
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from pydantic import BaseModel, SecretStr

import daily_digest
from daily_digest.config import SettingsError, load_llm_settings
from daily_digest.llm import (
    BudgetExceeded,
    MeteredClient,
    MockClient,
    OutputInvalidError,
    PermanentLLMError,
    TransientLLMError,
    Usage,
)
from daily_digest.llm.anthropic_client import API_BASE_URL, AnthropicClient
from daily_digest.llm.metered import cost_usd

SETTINGS = load_llm_settings()
TIGHT = SETTINGS.model_copy(update={"max_usd_per_run": Decimal("0.10")})  # the cap before the Writer was added
FAKE_KEY = "sk-ant-FAKE-test-key-0123456789"


class Answer(BaseModel):
    word: str


def ask(client, text: str = "hi", max_tokens: int = 4000):
    return client.complete(
        system="sys", messages=[{"role": "user", "content": text}], schema=Answer, max_tokens=max_tokens
    )


def metered(responses, settings=SETTINGS, **kwargs):
    inner = MockClient(responses, **kwargs)
    sleeps: list[float] = []
    return MeteredClient(inner, settings, sleep=sleeps.append), inner, sleeps


# --- cost math -------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("tokens_in", "tokens_out", "dollars"),
    [(1_000_000, 0, "2"), (0, 500_000, "5"), (12_000, 800, "0.032")],
    ids=["input-only", "output-only", "mixed"],
)
def test_cost_matches_hand_computed_examples(tokens_in, tokens_out, dollars):
    assert cost_usd(SETTINGS.prices["claude-sonnet-5-5"], tokens_in, tokens_out) == Decimal(dollars)


def test_usage_line_totals_tokens_and_dollars():
    client, _, _ = metered(['{"word": "a"}'], usage=Usage(input_tokens=12_000, output_tokens=800))
    ask(client)
    assert client.usage_line() == "usage: 1 call(s), 12,000 tokens in, 800 out, $0.0320"


# --- budget ----------------------------------------------------------------------------

def test_worst_case_over_the_dollar_cap_blocks_the_call():
    client, inner, _ = metered(['{"word": "a"}'], settings=TIGHT)
    # about 31k estimated input tokens plus 4k output is roughly $0.10 at the default prices
    with pytest.raises(BudgetExceeded, match="run cap"):
        ask(client, "x" * 93_000)
    assert inner.requests == []
    assert client.calls == 0


def test_token_ceilings_block_the_call():
    client, inner, _ = metered([])
    with pytest.raises(BudgetExceeded, match="input tokens"):
        ask(client, "x" * 130_000)
    with pytest.raises(BudgetExceeded, match="output tokens"):
        ask(client, max_tokens=SETTINGS.max_output_tokens + 1)
    assert inner.requests == []


def test_retries_count_toward_the_cap():
    tight = SETTINGS.model_copy(update={"max_usd_per_run": Decimal("0.05")})
    failure = TransientLLMError("boom", Usage(input_tokens=5_000, output_tokens=4_000))
    client, inner, _ = metered([failure, '{"word": "a"}'], settings=tight)
    with pytest.raises(BudgetExceeded):  # $0.05 spent by the failed attempt; no room for another
        ask(client)
    assert len(inner.requests) == 1
    assert client.spent == Decimal("0.05")


# --- retries and logging ---------------------------------------------------------------

def test_transient_errors_retry_twice_then_succeed():
    client, inner, sleeps = metered([TransientLLMError("429"), TransientLLMError("500"), '{"word": "a"}'])
    assert ask(client).parsed.word == "a"
    assert len(inner.requests) == 3 and sleeps == [1.0, 2.0]
    assert client.calls == 3


def test_transient_errors_give_up_after_two_retries():
    client, inner, _ = metered([TransientLLMError("x")] * 3)
    with pytest.raises(TransientLLMError):
        ask(client)
    assert len(inner.requests) == 3


@pytest.mark.parametrize("error", [PermanentLLMError("401"), OutputInvalidError("bad json")])
def test_permanent_and_invalid_errors_do_not_retry(error):
    client, inner, _ = metered([error, '{"word": "a"}'])
    with pytest.raises(type(error)):
        ask(client)
    assert len(inner.requests) == 1


def test_every_call_is_logged_with_the_required_fields(caplog):
    caplog.set_level(logging.DEBUG, logger="daily_digest.llm")
    client, _, _ = metered([TransientLLMError("429"), '{"word": "a"}'], settings=TIGHT, usage=Usage(input_tokens=2_000, output_tokens=100))
    ask(client, "secret prompt body")
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2
    assert "attempt=1 outcome=transient_error input_tokens=0" in lines[0]
    for field in ("model=claude-sonnet-5-5", "attempt=2", "outcome=ok", "input_tokens=2000",
                  "output_tokens=100", "call_usd=$0.0050", "cumulative_usd=$0.0050", "remaining_usd=$0.0950"):
        assert field in lines[1]
    assert "secret prompt body" not in caplog.text


def test_failed_and_blocked_calls_are_logged_too(caplog):
    caplog.set_level(logging.DEBUG, logger="daily_digest.llm")
    client, _, _ = metered([PermanentLLMError("401", Usage(input_tokens=10))])
    with pytest.raises(PermanentLLMError):
        ask(client)
    with pytest.raises(BudgetExceeded):
        ask(client, max_tokens=999_999)
    assert "outcome=permanent_error" in caplog.records[0].getMessage()
    assert "outcome=blocked_by_budget" in caplog.records[1].getMessage()


# --- Anthropic adapter, mocked at the HTTP layer ---------------------------------------

def message_body(text: str, *, stop_reason="end_turn", usage=(120, 30)) -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": text}], "stop_reason": stop_reason,
        "stop_sequence": None, "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
    }


def adapter(handler) -> tuple[AnthropicClient, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def record(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    client = AnthropicClient(
        api_key=SecretStr(FAKE_KEY), model="claude-sonnet-5-5",
        timeout_seconds=5, http_client=httpx2.Client(transport=httpx2.MockTransport(record)),
    )
    return client, seen


def test_request_shape_and_usage_parsing():
    client, seen = adapter(lambda r: httpx2.Response(200, json=message_body('{"word": "hello"}')))
    result = ask(client, "the prompt")
    assert result.parsed == Answer(word="hello")
    assert result.usage == Usage(input_tokens=120, output_tokens=30)
    request = seen[0]
    body = json.loads(request.content)
    assert str(request.url) == f"{API_BASE_URL}/v1/messages"
    assert request.headers["x-api-key"] == FAKE_KEY
    assert body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 4000
    assert body["system"] == "sys" and body["messages"] == [{"role": "user", "content": "the prompt"}]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert not {"temperature", "top_p", "top_k", "tool_choice", "thinking"} & body.keys()


@pytest.mark.parametrize("status", [408, 429, 500, 529])
def test_temporary_http_errors_are_transient_and_not_retried_by_the_sdk(status):
    client, seen = adapter(lambda r: httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "m"}}))
    with pytest.raises(TransientLLMError, match=str(status)):
        ask(client)
    assert len(seen) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_http_errors_are_permanent(status):
    client, seen = adapter(lambda r: httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "m"}}))
    with pytest.raises(PermanentLLMError, match=str(status)):
        ask(client)
    assert len(seen) == 1


def test_timeouts_and_connection_failures_are_transient():
    def timeout(request):
        raise httpx2.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx2.ConnectError("no route", request=request)

    for handler in (timeout, refused):
        client, seen = adapter(handler)
        with pytest.raises(TransientLLMError):
            ask(client)
        assert len(seen) == 1


def test_refusal_truncation_and_bad_json_keep_their_usage():
    for body, error in [
        (message_body("", stop_reason="refusal"), PermanentLLMError),
        (message_body('{"wor', stop_reason="max_tokens", usage=(50, 4000)), PermanentLLMError),
        (message_body("not json", usage=(77, 5)), OutputInvalidError),
    ]:
        client, _ = adapter(lambda r, body=body: httpx2.Response(200, json=body))
        with pytest.raises(error) as caught:
            ask(client)
        assert caught.value.usage.input_tokens == body["usage"]["input_tokens"]


def test_client_has_no_sdk_retries_and_a_pinned_base_url(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://evil.example.com")
    client = AnthropicClient(
        api_key=SecretStr(FAKE_KEY), model="m", timeout_seconds=7
    )
    assert client._client.max_retries == 0
    assert str(client._client.base_url).rstrip("/") == API_BASE_URL
    assert client._client.timeout == 7


def test_key_never_appears_in_settings_reprs_or_errors():
    def echo_key(request):
        return httpx2.Response(401, json={"error": {"message": f"bad key {request.headers['x-api-key']}"}})

    client, _ = adapter(echo_key)
    with pytest.raises(PermanentLLMError) as caught:
        ask(client)
    rendered = "".join(traceback.format_exception(caught.value)) + repr(caught.value)
    metered_client = MeteredClient(client, SETTINGS)
    reprs = (repr(SETTINGS), repr(SecretStr(FAKE_KEY)), repr(client), repr(client._client), repr(metered_client))
    for text in (rendered, *reprs, metered_client.usage_line()):
        assert FAKE_KEY not in text


# --- structure -------------------------------------------------------------------------

def test_only_the_llm_package_imports_the_anthropic_sdk():
    root = Path(daily_digest.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            )
            if any(n == "anthropic" or n.startswith("anthropic.") for n in names):
                offenders.append(path.relative_to(root).parts[0])
    assert set(offenders) == {"llm"}


# --- settings --------------------------------------------------------------------------

def write_settings(tmp_path, **changes):
    values = {"provider": '"anthropic"', "model": '"claude-sonnet-5-5"', "max_usd_per_run": "0.10",
              "max_input_tokens": "40000", "max_output_tokens": "4000", "request_timeout_seconds": "60"}
    values.update(changes)
    lines = [f"{k} = {v}" for k, v in values.items() if v is not None]
    text = "[llm]\n" + "\n".join(lines) + '\n[llm.prices.claude-sonnet-5-5]\ninput_per_mtok = 2.0\noutput_per_mtok = 10.0\n'
    path = tmp_path / "settings.toml"
    path.write_text(text)
    return path


def test_shipped_settings_match_the_documented_defaults():
    assert SETTINGS.model == "claude-sonnet-5-5"
    assert (SETTINGS.max_usd_per_run, SETTINGS.max_input_tokens, SETTINGS.max_output_tokens) == (
        Decimal("0.25"), 40_000, 6_000)
    prices = SETTINGS.prices["claude-sonnet-5-5"]
    assert (prices.input_per_mtok, prices.output_per_mtok) == (2, 10)


@pytest.mark.parametrize(
    ("changes", "key"),
    [
        ({"max_usd_per_run": None}, "llm.max_usd_per_run"),
        ({"max_usd_per_run": "-1"}, "llm.max_usd_per_run"),
        ({"max_output_tokens": '"lots"'}, "llm.max_output_tokens"),
        ({"provider": '"openai"'}, "llm.provider"),
        ({"max_usd": "1"}, "llm.max_usd"),
        ({"model": '"unpriced-model"'}, "unpriced-model"),
    ],
)
def test_bad_settings_fail_naming_the_key(tmp_path, changes, key):
    with pytest.raises(SettingsError, match=key):
        load_llm_settings(write_settings(tmp_path, **changes))


def test_missing_settings_file_is_reported(tmp_path):
    with pytest.raises(SettingsError, match="not found"):
        load_llm_settings(tmp_path / "nope.toml")
