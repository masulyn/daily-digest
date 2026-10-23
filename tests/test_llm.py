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
from daily_digest.llm.metered import cost_usd, estimate_input_tokens

SETTINGS = load_llm_settings()
TIGHT = SETTINGS.model_copy(update={"max_usd_per_run": Decimal("0.10")})  # small enough for one call to reach
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
    # about 35k estimated input tokens plus 4k output is just over $0.10 at the default prices
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


def test_retry_that_would_cross_the_run_input_ceiling_is_blocked():
    estimate = estimate_input_tokens("sys", [{"role": "user", "content": "hi"}], Answer)
    tight = SETTINGS.model_copy(update={"max_run_input_tokens": 1_000 + estimate - 1})
    failure = TransientLLMError("boom", Usage(input_tokens=1_000, output_tokens=10))
    client, inner, _ = metered([failure, '{"word": "a"}'], settings=tight)
    with pytest.raises(BudgetExceeded, match="run ceiling"):
        ask(client)
    assert len(inner.requests) == 1


def test_retry_that_would_cross_the_run_output_ceiling_is_blocked(caplog):
    caplog.set_level(logging.DEBUG, logger="daily_digest.llm")
    tight = SETTINGS.model_copy(update={"max_run_output_tokens": 4_000 + 500 - 1})
    failure = TransientLLMError("boom", Usage(input_tokens=10, output_tokens=500))
    client, inner, _ = metered([failure, '{"word": "a"}'], settings=tight)
    with pytest.raises(BudgetExceeded, match="output tokens.*run ceiling"):
        ask(client)
    assert len(inner.requests) == 1
    assert "outcome=blocked_by_budget" in caplog.records[-1].getMessage()


def test_a_retry_within_the_run_ceilings_goes_ahead():
    failure = TransientLLMError("boom", Usage(input_tokens=1_000, output_tokens=10))
    client, _, _ = metered([failure, '{"word": "a"}'])
    assert ask(client).parsed.word == "a"


# --- attempts with unknown usage ---------------------------------------------------------

def test_timeout_then_success_counts_the_worst_case_for_the_timeout(caplog):
    caplog.set_level(logging.DEBUG, logger="daily_digest.llm")
    estimate = estimate_input_tokens("sys", [{"role": "user", "content": "hi"}], Answer)
    unknown = TransientLLMError("timed out", usage_known=False)
    client, _, _ = metered([unknown, '{"word": "a"}'], usage=Usage(input_tokens=100, output_tokens=50))
    ask(client)
    assert client.calls == 2
    assert client.input_tokens == estimate + 100 and client.output_tokens == 4_000 + 50
    assert client.spent == cost_usd(SETTINGS.prices["claude-sonnet-5-5"], estimate + 100, 4_050)
    assert client.usage_line().endswith(
        "; includes 1 attempt(s) with unknown usage counted at the worst-case estimate")
    first = caplog.records[0].getMessage()
    assert "attempt=1 outcome=transient_error_usage_unknown" in first
    assert f"input_tokens={estimate} output_tokens=4000" in first


def test_status_error_then_success_adds_nothing_for_the_failed_attempt():
    client, _, _ = metered([TransientLLMError("429"), '{"word": "a"}'], usage=Usage(input_tokens=100, output_tokens=50))
    ask(client)
    assert (client.input_tokens, client.output_tokens) == (100, 50)
    assert client.spent == cost_usd(SETTINGS.prices["claude-sonnet-5-5"], 100, 50)
    assert "unknown usage" not in client.usage_line()


def test_an_unknown_usage_attempt_can_push_a_later_attempt_over_the_dollar_cap():
    # one worst-case attempt is about $0.04; a $0.05 cap leaves no room for a second
    tight = SETTINGS.model_copy(update={"max_usd_per_run": Decimal("0.05")})
    client, inner, _ = metered([TransientLLMError("timed out", usage_known=False), '{"word": "a"}'], settings=tight)
    with pytest.raises(BudgetExceeded, match="run cap"):
        ask(client)
    assert len(inner.requests) == 1
    assert client.spent > Decimal("0.04")


def test_unknown_usage_attempt_counts_its_output_toward_the_run_output_ceiling():
    # Counting only input would leave 4,000 output tokens unaccounted and let the retry through.
    tight = SETTINGS.model_copy(update={"max_run_output_tokens": 4_000 + 4_000 - 1})
    client, inner, _ = metered([TransientLLMError("timed out", usage_known=False), '{"word": "a"}'], settings=tight)
    with pytest.raises(BudgetExceeded, match="output tokens.*run ceiling"):
        ask(client)
    assert len(inner.requests) == 1 and client.output_tokens == 4_000


def test_unknown_usage_attempt_counts_its_input_toward_the_run_input_ceiling():
    estimate = estimate_input_tokens("sys", [{"role": "user", "content": "hi"}], Answer)
    tight = SETTINGS.model_copy(update={"max_run_input_tokens": 2 * estimate - 1})
    client, inner, _ = metered([TransientLLMError("timed out", usage_known=False), '{"word": "a"}'], settings=tight)
    with pytest.raises(BudgetExceeded, match="input tokens.*run ceiling"):
        ask(client)
    assert len(inner.requests) == 1 and client.input_tokens == estimate


def test_run_ceilings_allow_an_attempt_that_lands_exactly_on_them():
    estimate = estimate_input_tokens("sys", [{"role": "user", "content": "hi"}], Answer)
    exact = SETTINGS.model_copy(
        update={"max_run_input_tokens": 2 * estimate, "max_run_output_tokens": 2 * 4_000}
    )
    client, inner, _ = metered([TransientLLMError("timed out", usage_known=False), '{"word": "a"}'], settings=exact)
    assert ask(client).parsed.word == "a"
    assert len(inner.requests) == 2


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


@pytest.mark.parametrize(
    ("status", "usage_known"), [(409, True), (429, True), (500, True), (529, True), (408, False), (504, False)]
)
def test_adapter_marks_only_possibly_billed_statuses_as_unknown_usage(status, usage_known):
    client, _ = adapter(lambda r: httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "m"}}))
    with pytest.raises(TransientLLMError) as caught:
        ask(client)
    assert caught.value.usage_known is usage_known


@pytest.mark.parametrize(
    ("raised", "usage_known"),
    [
        (httpx2.ConnectError("no route"), True),
        (httpx2.ConnectTimeout("slow connect"), True),
        (httpx2.ReadTimeout("slow"), False),
        (httpx2.WriteError("broken pipe"), False),
        (httpx2.RemoteProtocolError("dropped"), False),
    ],
    ids=["connect-error", "connect-timeout", "read-timeout", "write-error", "dropped"],
)
def test_network_failures_are_transient_and_known_zero_only_if_never_sent(raised, usage_known):
    def handler(request):
        raise raised

    client, seen = adapter(handler)
    with pytest.raises(TransientLLMError) as caught:
        ask(client)
    assert len(seen) == 1
    assert caught.value.usage_known is usage_known


def test_flaky_network_that_never_connects_costs_nothing_extra():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) <= 2:
            raise httpx2.ConnectError("no route", request=request)
        return httpx2.Response(200, json=message_body('{"word": "a"}', usage=(120, 30)))

    client, _ = adapter(handler)
    metered_client = MeteredClient(client, SETTINGS, sleep=lambda seconds: None)
    ask(metered_client)
    assert metered_client.spent == cost_usd(SETTINGS.prices["claude-sonnet-5-5"], 120, 30)
    assert "unknown usage" not in metered_client.usage_line()


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
              "max_input_tokens": "40000", "max_output_tokens": "4000",
              "max_run_input_tokens": "150000", "max_run_output_tokens": "20000", "max_review_rounds": "3",
              "low_volume_max_items": "8", "low_volume_min_words": "50", "low_volume_aim_words": "100",
              "request_timeout_seconds": "60"}
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
    assert (SETTINGS.max_run_input_tokens, SETTINGS.max_run_output_tokens) == (150_000, 20_000)
    assert SETTINGS.max_review_rounds == 3
    assert (SETTINGS.low_volume_max_items, SETTINGS.low_volume_min_words, SETTINGS.low_volume_aim_words) == (8, 50, 100)
    assert SETTINGS.max_run_input_tokens >= SETTINGS.max_input_tokens
    assert SETTINGS.max_run_output_tokens >= SETTINGS.max_output_tokens
    prices = SETTINGS.prices["claude-sonnet-5-5"]
    assert (prices.input_per_mtok, prices.output_per_mtok) == (2, 10)


@pytest.mark.parametrize(
    ("changes", "key"),
    [
        ({"max_usd_per_run": None}, "llm.max_usd_per_run"),
        ({"max_usd_per_run": "-1"}, "llm.max_usd_per_run"),
        ({"max_output_tokens": '"lots"'}, "llm.max_output_tokens"),
        ({"max_run_input_tokens": None}, "llm.max_run_input_tokens"),
        ({"max_run_input_tokens": "39999"}, "llm.max_run_input_tokens"),
        ({"max_run_output_tokens": "3999"}, "llm.max_run_output_tokens"),
        ({"max_review_rounds": None}, "llm.max_review_rounds"),
        ({"max_review_rounds": "0"}, "llm.max_review_rounds"),
        ({"max_review_rounds": "-2"}, "llm.max_review_rounds"),
        ({"max_review_rounds": '"three"'}, "llm.max_review_rounds"),
        ({"low_volume_max_items": None}, "llm.low_volume_max_items"),
        ({"low_volume_max_items": "0"}, "llm.low_volume_max_items"),
        ({"low_volume_max_items": '"eight"'}, "llm.low_volume_max_items"),
        ({"low_volume_min_words": None}, "llm.low_volume_min_words"),
        ({"low_volume_min_words": "0"}, "llm.low_volume_min_words"),
        ({"low_volume_min_words": "200"}, "llm.low_volume_min_words"),
        ({"low_volume_min_words": "100.5"}, "llm.low_volume_min_words"),
        ({"low_volume_aim_words": None}, "llm.low_volume_aim_words"),
        ({"low_volume_aim_words": "0"}, "llm.low_volume_aim_words"),
        ({"low_volume_aim_words": "200"}, "llm.low_volume_aim_words"),
        ({"low_volume_aim_words": "49"}, "llm.low_volume_aim_words"),  # below the minimum of 50
        ({"low_volume_aim_words": '"about 100"'}, "llm.low_volume_aim_words"),
        ({"low_volume_min_words": "120", "low_volume_aim_words": "100"}, "llm.low_volume_aim_words"),
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


def test_the_input_estimate_adds_the_output_format_and_sits_just_above_one_measured_prompt():
    import json
    import math

    from daily_digest.contracts import Draft
    from daily_digest.llm.metered import estimate_input_tokens

    text = "x" * 34_154  # the Oct 2026 digest prompt: system plus 20 items, 12,572 input tokens measured
    schema_chars = len(json.dumps(Draft.model_json_schema()))
    assert estimate_input_tokens("", [{"content": text}], Draft) == math.ceil((34_154 + schema_chars) / 2.7)
    assert estimate_input_tokens("", [{"content": text}]) == math.ceil(34_154 / 2.7)
    assert 12_572 <= estimate_input_tokens("", [{"content": text}], Draft) <= 12_572 * 1.15  # one data point
