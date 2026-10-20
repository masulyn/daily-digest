import sys
from datetime import date

import httpx2
import pytest
from pydantic import SecretStr

from daily_digest import cli
from daily_digest.contracts import ReviewIssue, ReviewResult
from daily_digest.llm.anthropic_client import AnthropicClient
from daily_digest.store import Store
from draft_helpers import HEADLINE, make_draft
from helpers import editor_items

DAY = date(2026, 10, 14)
FAKE_KEY = "sk-ant-FAKE-test-key-0123456789"
REQUEST_USAGE = (2400, 700)


def issue(index=1, change="Remove the unsupported claim.", severity="blocking", category="unsupported"):
    return ReviewIssue(section="what_you_need_to_know", index=index, severity=severity, category=category,
                       required_change=change)


def blocked(**kwargs) -> ReviewResult:
    return ReviewResult(issues=[issue(**kwargs)], verdict="revise")


APPROVE = ReviewResult(verdict="approve")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "digest.db"
    with Store(path) as store:
        for item in editor_items(DAY, "a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8"):
            store.add_if_new(item)
            if item.article_text:
                store.save_article_text(item.id, item.article_text)
    return path


def script(monkeypatch, replies):
    """Serve the given models (or (model, usage) pairs) in order as the API's replies."""
    queue = list(replies)

    def handler(request):
        reply = queue.pop(0)
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


def draft_cli(monkeypatch, db, *extra):
    monkeypatch.setattr(sys, "argv", ["daily-digest", "draft", "--db", str(db), "--date", DAY.isoformat(), *extra])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    return exit_info.value.code


def test_an_approved_digest_prints_the_review_line_after_the_sources_and_exits_zero(db, monkeypatch, capsys):
    script(monkeypatch, [make_draft(300), APPROVE])
    assert draft_cli(monkeypatch, db) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"# {HEADLINE}" and "NOT REVIEWED" not in "\n".join(lines)
    at = lines.index("review: approved after 1 round(s)")
    assert lines[at - 1] == "" and lines[at - 2].startswith("3. [")
    assert lines[-1].startswith("usage: 2 call(s)")


def test_advisory_issues_are_listed_as_bullets(db, monkeypatch, capsys):
    advisory = ReviewResult(issues=[issue(index=0, severity="advisory", category="tone", change="Shorten this.")])
    script(monkeypatch, [make_draft(300), advisory])
    assert draft_cli(monkeypatch, db) == 0
    out = capsys.readouterr().out
    assert "review: approved after 1 round(s)\n  - advisory: what_you_need_to_know sentence 1 (tone):" in out
    assert "Shorten this." in out


def test_a_digest_that_never_converges_loses_the_flagged_sentence_and_says_so(db, monkeypatch, capsys):
    script(monkeypatch, [make_draft(300), blocked(), make_draft(300), blocked(), make_draft(300), blocked()])
    assert draft_cli(monkeypatch, db) == 0
    out = capsys.readouterr().out
    assert "review: not converged after 3 round(s); 1 sentence removed" in out
    assert "  - unresolved: what_you_need_to_know sentence 2 (unsupported): \"A survey found" in out
    markdown = out.split("review:")[0]
    assert "A survey found" not in markdown and "NOT REVIEWED" not in out


def test_a_draft_that_cannot_be_repaired_prints_no_digest_and_exits_one(db, monkeypatch, capsys):
    script(monkeypatch, [make_draft(205), blocked(), make_draft(205), blocked(), make_draft(205), blocked()])
    assert draft_cli(monkeypatch, db) == 1
    out = capsys.readouterr().out
    assert f"# {HEADLINE}" not in out and "## Sources" not in out
    assert "review: FAILED after 3 round(s); the draft could not be repaired" in out
    assert "could not repair the draft" in out and out.splitlines()[-1].startswith("usage: 6 call(s)")


def test_no_review_prints_a_banner_and_exits_three_without_any_review_call(db, monkeypatch, capsys):
    queue = script(monkeypatch, [make_draft(300)])
    assert draft_cli(monkeypatch, db, "--no-review") == 3
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("NOT REVIEWED: this digest has not been checked and must not be published.")
    assert out.index("NOT REVIEWED") < out.index(f"# {HEADLINE}")
    assert "review: NOT REVIEWED" in out and out.splitlines()[-1].startswith("usage: 1 call(s)")
    assert queue == []


def test_a_budget_that_blocks_the_first_review_gives_the_same_unreviewed_outcome(db, monkeypatch, capsys):
    from decimal import Decimal

    settings = cli.load_llm_settings(cli.DEFAULT_SETTINGS_PATH).model_copy(update={"max_usd_per_run": Decimal("0.10")})
    monkeypatch.setattr(cli, "load_llm_settings", lambda path: settings)
    script(monkeypatch, [(make_draft(300), (30_000, 3_000))])  # $0.09: past 80% of $0.10
    assert draft_cli(monkeypatch, db) == 3
    out = capsys.readouterr().out
    assert "NOT REVIEWED" in out and "review: NOT REVIEWED" in out
    assert out.splitlines()[-1].startswith("usage: 1 call(s), 30,000 tokens in")


def test_reaching_80_percent_after_round_one_degrades_and_still_prints_the_usage_line(db, monkeypatch, capsys):
    heavy = (30_000, 1_000)  # $0.07 a call: draft, review and revision reach $0.21 of $0.25
    script(monkeypatch, [(make_draft(300), heavy), (blocked(), heavy), (make_draft(300), heavy)])
    assert draft_cli(monkeypatch, db) == 0
    out = capsys.readouterr().out
    assert "review: degraded at 80% of the budget after 1 round(s); 1 sentence removed" in out
    assert out.splitlines()[-1].startswith("usage: 3 call(s)")


def test_debug_round_lines_carry_numbers_and_labels_only(db, monkeypatch, capfd):
    script(monkeypatch, [make_draft(300), blocked(change="SECRET-REQUIRED-CHANGE"), make_draft(300), APPROVE])
    assert draft_cli(monkeypatch, db, "--debug") == 0
    err = capfd.readouterr().err
    assert "review round 1 mechanical=0 model=1 blocking=1 verdict=revise cumulative_usd=$0.0" in err
    assert "review round 2 mechanical=0 model=0 blocking=0 verdict=approve cumulative_usd=$0.0" in err
    for forbidden in ("SECRET-REQUIRED-CHANGE", "A survey found", "alpha", FAKE_KEY, HEADLINE):
        assert forbidden not in err


def test_control_characters_in_issue_text_never_reach_the_terminal(db, monkeypatch, capsys):
    script(monkeypatch, [make_draft(300), blocked(change="Remove \x1b[31mthis"), make_draft(300),
                         blocked(change="Remove \x1b[31mthis"), make_draft(300), blocked(change="Remove \x1b[31mthis")])
    assert draft_cli(monkeypatch, db) == 0
    captured = capsys.readouterr()
    assert "\x1b" not in captured.out and "\x1b" not in captured.err
    assert "Remove  [31mthis" in captured.out


def test_stub_runs_are_unchanged_and_need_no_key_or_review(db, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert draft_cli(monkeypatch, db, "--stub") == 0
    out = capsys.readouterr().out
    assert "review:" not in out and "usage:" not in out and "## " not in out
