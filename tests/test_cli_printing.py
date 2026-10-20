"""Nothing from a feed or a model reaches the terminal with control characters in it."""

import logging
import sys
from datetime import date, datetime

import httpx2
import pytest
from pydantic import SecretStr

from daily_digest import cli
from daily_digest.llm.anthropic_client import AnthropicClient
from daily_digest.store import Store
from draft_helpers import make_draft
from helpers import editor_items, sample_item

DAY = date(2026, 10, 14)
ESC = "\x1b[31m"


def noon() -> datetime:
    return datetime.combine(DAY, datetime.min.time().replace(hour=12)).astimezone()


def message_body(text: str) -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def fake_api(monkeypatch):
    def handler(request):
        return httpx2.Response(200, json=message_body(make_draft(250).model_dump_json()))

    def make(settings, api_key: SecretStr):
        return AnthropicClient(
            api_key=api_key, model=settings.model, timeout_seconds=5,
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )

    monkeypatch.setattr(cli, "_make_inner_client", make)


def run_main(monkeypatch, db, *extra) -> int:
    monkeypatch.setattr(sys, "argv", ["daily-digest", "draft", "--db", str(db), "--date", DAY.isoformat(), *extra])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    return exit_info.value.code


def store_with(path, *items):
    with Store(path) as store:
        for item in items:
            store.add_if_new(item)


def test_stub_draft_does_not_print_escape_sequences_from_the_url(tmp_path, capsys, monkeypatch):
    db = tmp_path / "digest.db"
    item = sample_item("a", "Headline fact. More.").model_copy(
        update={"canonical_url": f"https://news.example.com/a{ESC}x", "published": noon()}
    )
    store_with(db, item)
    assert run_main(monkeypatch, db, "--stub") == 0
    captured = capsys.readouterr()
    assert "\x1b" not in captured.out + captured.err and "https://news.example.com/a [31mx" in captured.out


def test_stub_draft_does_not_print_escape_sequences_from_source_or_title(tmp_path, capsys, monkeypatch):
    db = tmp_path / "digest.db"
    item = sample_item("a", "Headline fact. More.").model_copy(
        update={"source": f"Src{ESC}", "title": f"Ti{ESC}tle", "published": noon()}
    )
    store_with(db, item)
    assert run_main(monkeypatch, db, "--stub") == 0
    captured = capsys.readouterr()
    assert "\x1b" not in captured.out + captured.err and "Ti [31mtle (Src [31m)" in captured.out


def test_real_draft_does_not_print_escape_sequences_from_the_url(tmp_path, capsys, monkeypatch):
    db = tmp_path / "digest.db"
    items = [i.model_copy(update={"canonical_url": i.canonical_url + ESC}) for i in editor_items(DAY)]
    store_with(db, *items)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake-key")
    fake_api(monkeypatch)
    assert run_main(monkeypatch, db) == 0
    captured = capsys.readouterr()
    assert "\x1b" not in captured.out + captured.err and "[31m" in captured.out  # the URL is shown, just defanged


def test_debug_log_lines_are_cleaned(capsys):
    with cli._debug_logging(True):
        logging.getLogger("daily_digest").debug("saw %s", f"bad{ESC}url")
    captured = capsys.readouterr()
    assert "\x1b" not in captured.out + captured.err and "bad [31murl" in captured.err


def test_error_lines_are_cleaned(tmp_path, capsys):
    assert cli.run_draft(tmp_path / f"missing{ESC}.db", DAY) == 2
    captured = capsys.readouterr()
    assert "\x1b" not in captured.out + captured.err and "missing [31m.db" in captured.err


def test_settings_error_is_cleaned(tmp_path, capsys, monkeypatch):
    db = tmp_path / "digest.db"
    store_with(db, sample_item("a"))
    monkeypatch.setattr(cli, "DEFAULT_SETTINGS_PATH", tmp_path / f"settings{ESC}.toml")
    assert run_main(monkeypatch, db) == 2
    captured = capsys.readouterr()
    assert "\x1b" not in captured.out + captured.err and "settings [31m.toml" in captured.err
