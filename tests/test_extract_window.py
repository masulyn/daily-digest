import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from daily_digest import cli
from daily_digest import extract as extract_module
from daily_digest.robots import RobotsChecker
from daily_digest.store import _MIGRATIONS, Store
from helpers import TEST_SOURCES, guarded_fetch, sample_item

PAGE = (Path(__file__).parent / "fixtures" / "article_page.html").read_bytes()
NOON = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
SINCE = NOON - timedelta(days=1)


def extract_articles(*args, **kwargs):
    return extract_module.extract_articles(*args, sources=TEST_SOURCES, **kwargs)


def allow_all() -> RobotsChecker:
    return RobotsChecker(lambda url: (404, b""), "test")


def window_store() -> Store:
    store = Store(":memory:")
    for slug, published in [
        ("new", NOON),
        ("edge", SINCE),
        ("old", SINCE - timedelta(seconds=1)),
        ("older", SINCE - timedelta(days=30)),
    ]:
        store.add_if_new(sample_item(slug).model_copy(update={"published": published}), needs_extraction=True)
    return store


def ids(pairs):
    return [i for i, _ in pairs]


def test_since_excludes_older_items_includes_the_boundary_and_keeps_newest_first():
    store = window_store()
    assert ids(store.pending_extraction(since=SINCE)) == ["new", "edge"]
    assert ids(store.pending_extraction()) == ["new", "edge", "old", "older"]  # nothing was changed
    assert ids(store.pending_extraction(since=SINCE - timedelta(days=60))) == ["new", "edge", "old", "older"]


def test_limit_applies_after_the_window_filter():
    assert ids(window_store().pending_extraction(limit=1, since=SINCE)) == ["new"]
    assert ids(window_store().pending_extraction(limit=5, since=SINCE)) == ["new", "edge"]


def test_extract_articles_fetches_only_items_in_the_window():
    store = window_store()
    fetch = guarded_fetch(lambda url: (200, PAGE))
    report = extract_articles(store, fetch, allow_all(), since=SINCE)
    assert fetch.calls == ["https://news.example.com/new", "https://news.example.com/edge"]
    assert report.considered == 2
    assert ids(store.pending_extraction()) == ["old", "older"]


@pytest.mark.parametrize(
    ("today", "days", "first_day"),
    [
        (date(2026, 10, 20), 1, date(2026, 10, 20)),
        (date(2026, 10, 20), 2, date(2026, 10, 19)),
        (date(2026, 10, 20), 7, date(2026, 10, 14)),
        (date(2026, 11, 2), 4, date(2026, 10, 30)),  # across a month boundary
    ],
)
def test_since_is_local_midnight_of_the_first_day(today, days, first_day):
    since = cli.extract_since(today, days)
    assert since == datetime.combine(first_day, datetime.min.time()).astimezone()


def _run_cli(monkeypatch, tmp_path, *argv):
    calls = []
    monkeypatch.setattr(cli, "run_extract", lambda db, limit, days, feeds=None: calls.append(days) or 0)
    monkeypatch.setattr("sys.argv", ["daily-digest", "extract", "--db", str(tmp_path / "x.db"), *argv])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    return exit_info.value.code, calls


def test_days_defaults_to_two_and_a_wider_value_widens_the_window(monkeypatch, tmp_path):
    _, default = _run_cli(monkeypatch, tmp_path)
    _, wide = _run_cli(monkeypatch, tmp_path, "--days", "7")
    assert default == [2] and wide == [7]
    assert cli.extract_since(date.today(), 7) < cli.extract_since(date.today(), 2)


@pytest.mark.parametrize("value", ["0", "-1", "x"])
def test_days_below_one_is_rejected(monkeypatch, tmp_path, capsys, value):
    code, calls = _run_cli(monkeypatch, tmp_path, "--days", value)
    assert code == 2 and calls == []
    assert "--days" in capsys.readouterr().err


def test_window_line_is_printed_before_the_summary(monkeypatch, tmp_path, capsys):
    path = tmp_path / "digest.db"
    Store(path).close()
    monkeypatch.setattr("sys.argv", ["daily-digest", "extract", "--db", str(path), "--days", "3"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    since = cli.extract_since(date.today(), 3).date().isoformat()
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"Window: items published since {since} (--days 3)."
    assert lines[1].startswith("Considered 0, extracted 0")


def test_migration_flagged_old_row_is_fetched_only_with_a_wide_window(tmp_path):
    path = tmp_path / "v3.db"
    old = sqlite3.connect(path)
    for script in _MIGRATIONS[:3]:
        old.executescript(script)
    old.execute(
        "INSERT INTO items (id, source, canonical_url, content_hash, title, published_at, extract, first_seen_at)"
        " VALUES ('cut', 's', 'https://e.com/cut', 'h', 't', '2026-06-01T12:00:00+00:00', ?, 'x')",
        ("word " * 230 + "end…",),
    )
    old.commit()
    old.close()
    with Store(path) as store:
        fetch = guarded_fetch(lambda url: (200, PAGE))
        extract_articles(store, fetch, allow_all(), since=cli.extract_since(date.today(), 2))
        assert fetch.calls == []
        extract_articles(store, fetch, allow_all(), since=datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert fetch.calls == ["https://e.com/cut"]
