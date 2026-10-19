import subprocess
import sys
from datetime import date, datetime, timedelta

import pytest
from pydantic import ValidationError

from daily_digest.cli import run_draft
from daily_digest.contracts import ItemSummary
from daily_digest.graph import DigestState, day_window, load_items, run_digest, summarize_stub
from daily_digest.store import Store
from helpers import sample_item

DAY = date(2026, 10, 14)


def item_on(slug: str, when: datetime, extract: str = "", **fields):
    return sample_item(slug, extract).model_copy(update={"published": when, **fields})


def noon(day: date = DAY) -> datetime:
    return datetime.combine(day, datetime.min.time().replace(hour=12)).astimezone()


def test_window_is_local_midnight_to_local_midnight():
    start, end = day_window(DAY)
    assert start.replace(tzinfo=None) == datetime(2026, 10, 14)
    assert end.replace(tzinfo=None) == datetime(2026, 10, 15)


def test_load_items_returns_only_the_days_unseen_items():
    store = Store(":memory:")
    inside, seen = item_on("in", noon()), item_on("seen", noon())
    outside = item_on("out", noon(DAY + timedelta(days=1)))
    for item in (inside, seen, outside):
        store.add_if_new(item)
    store.mark_seen([seen], DAY)
    update = load_items(DigestState(target_date=DAY), store)
    assert [i.id for i in update["items"]] == ["in"]
    assert "notes" not in update


def test_window_includes_its_start_and_excludes_its_end():
    store = Store(":memory:")
    start, end = day_window(DAY)
    store.add_if_new(item_on("start", start))
    store.add_if_new(item_on("end", end))
    update = load_items(DigestState(target_date=DAY), store)
    assert [i.id for i in update["items"]] == ["start"]


def test_load_items_notes_a_quiet_day():
    update = load_items(DigestState(target_date=DAY), Store(":memory:"))
    assert update["items"] == []
    assert "quiet day" in update["notes"][0]


def test_summarize_stub_uses_first_sentence_of_article_text_over_extract():
    item = item_on("a", noon(), "Feed teaser.", article_text="First point. Second point.")
    [summary] = summarize_stub(DigestState(target_date=DAY, items=[item]))["summaries"]
    assert summary == ItemSummary(
        item_id="a", key_points=["First point."], source_url=item.canonical_url
    )


def test_summarize_stub_falls_back_to_extract_then_title():
    with_extract = item_on("a", noon(), "Only the extract.\nMore.")
    bare = item_on("b", noon())
    state = DigestState(target_date=DAY, items=[with_extract, bare])
    points = [s.key_points for s in summarize_stub(state)["summaries"]]
    assert points == [["Only the extract."], ["Story b"]]


def test_graph_summarizes_each_item_with_its_url():
    store = Store(":memory:")
    for slug in "abc":
        store.add_if_new(item_on(slug, noon(), f"Point {slug}. Detail."))
    state = run_digest(store, DAY)
    assert {s.source_url for s in state.summaries} == {
        f"https://news.example.com/{slug}" for slug in "abc"
    }
    assert state.notes == []


def test_graph_ends_cleanly_on_an_empty_day():
    state = run_digest(Store(":memory:"), DAY)
    assert state.summaries == []
    assert "quiet day" in state.notes[0]


def test_invalid_node_output_fails_loudly(monkeypatch):
    monkeypatch.setattr("daily_digest.graph.summarize_stub", lambda state: {"summaries": [{"item_id": "a"}]})
    with pytest.raises(ValidationError):
        run_digest(Store(":memory:"), DAY)


def test_draft_command_prints_summaries_and_quiet_day(tmp_path, capsys):
    path = tmp_path / "digest.db"
    with Store(path) as store:
        store.add_if_new(item_on("a", noon(), "Headline fact. More.", source="CIO.com AI"))
    assert run_draft(path, DAY) == 0
    out = capsys.readouterr().out
    assert "Story a (CIO.com AI)" in out
    assert "- Headline fact." in out and "https://news.example.com/a" in out
    assert run_draft(path, DAY - timedelta(days=5)) == 0
    assert "quiet day" in capsys.readouterr().out


def test_graph_module_does_not_load_the_article_parser():
    code = "import sys, daily_digest.graph; assert 'trafilatura' not in sys.modules"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
