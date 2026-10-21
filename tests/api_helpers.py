"""A scripted fake of the model API, shared by the CLI tests that run the whole pipeline."""

import httpx2
from pydantic import SecretStr

from daily_digest import cli
from daily_digest.contracts import ReviewIssue, ReviewResult
from daily_digest.llm.anthropic_client import AnthropicClient

FAKE_KEY = "sk-ant-FAKE-test-key-0123456789"
REQUEST_USAGE = (2400, 700)


def issue(index=1, change="Remove the unsupported claim.", severity="blocking", category="unsupported"):
    return ReviewIssue(section="what_you_need_to_know", index=index, severity=severity, category=category,
                       required_change=change)


def blocked(**kwargs) -> ReviewResult:
    return ReviewResult(issues=[issue(**kwargs)], verdict="revise")


APPROVE = ReviewResult(verdict="approve")


def script(monkeypatch, replies):
    """Serve the given models (or (model, usage) pairs) in order as the API's replies."""
    queue = list(replies)

    def handler(request):
        reply = queue.pop(0)
        if isinstance(reply, httpx2.Response):
            return reply
        model, usage = reply if isinstance(reply, tuple) else (reply, REQUEST_USAGE)
        body = {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": model.model_dump_json()}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
        }
        return httpx2.Response(200, json=body)

    def make(settings, api_key: SecretStr):
        return AnthropicClient(
            api_key=api_key, model=settings.model, timeout_seconds=5,
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setattr(cli, "_make_inner_client", make)
    return queue
