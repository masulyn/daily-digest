import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from api_helpers import APPROVE, FAKE_KEY, blocked, script
from daily_digest import cli
from daily_digest.contracts import CitedSource, PublishedDigest, ReviewSummary, UsageRecord
from daily_digest.digest import build_digest, render_markdown
from daily_digest.graph import DigestState
from daily_digest.publisher import (
    AlreadyExists,
    markdown_for,
    provenance_for,
    rebuild_index,
    save_digest,
    validate_digest,
)
from daily_digest.store import Store
from draft_helpers import HEADLINE, make_draft
from helpers import editor_items

DAY = date(2026, 10, 14)
IDS = ("a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8")
NOW = datetime(2026, 10, 15, 8, 30, tzinfo=timezone.utc)
APPROVED = ReviewSummary(status="approved", rounds=1)


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "digest.db"
    with Store(db) as store:
        for item in editor_items(DAY, *IDS):
            store.add_if_new(item)
            if item.article_text:
                store.save_article_text(item.id, item.article_text)
    return db, tmp_path / "newsletters"


def seen_rows(db: Path) -> int:
    with Store(db) as store:
        return store._db.execute("select count(*) from seen_items").fetchone()[0]


def generate(monkeypatch, env, *extra, day: date | None = DAY) -> int:
    db, out = env
    argv = ["daily-digest", "generate", "--db", str(db), "--out-dir", str(out), *extra]
    if day:
        argv += ["--date", day.isoformat()]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    return exit_info.value.code


def files(out: Path) -> list[str]:
    return sorted(p.name for p in out.iterdir()) if out.exists() else []


def published_for(day: date, headline: str = HEADLINE) -> PublishedDigest:
    draft = make_draft(300, headline=headline)
    digest = build_digest(draft, editor_items(day, *IDS), day, NOW, [], APPROVED)
    usage = UsageRecord(model="m", calls=2, input_tokens=1, output_tokens=1, usd="0.01")
    return PublishedDigest(digest=digest, provenance=provenance_for(draft), usage=usage)


# --- saving ----------------------------------------------------------------------------

def test_an_approved_digest_is_saved_indexed_and_its_items_are_marked_seen(monkeypatch, env, capsys):
    db, out = env
    script(monkeypatch, [make_draft(300), APPROVE])
    assert generate(monkeypatch, env) == 0
    assert files(out) == ["2026-10-14.json", "2026-10-14.md", "index.md"]
    published = PublishedDigest.model_validate_json((out / "2026-10-14.json").read_text())
    markdown = (out / "2026-10-14.md").read_text()
    assert markdown == markdown_for(published.digest)
    assert markdown.startswith(render_markdown(published.digest))
    assert markdown.endswith("\n\n*Review: approved after 1 round(s).*\n")
    assert len(published.provenance) == 5 and published.provenance[3].type == "quote"
    assert published.provenance[3].speaker == "Dana Reyes" and published.provenance[3].quote
    assert published.usage.calls == 2 and published.usage.model == "claude-sonnet-5-5"
    assert published.digest.review.status == "approved"
    index = (out / "index.md").read_text()
    assert f"[{HEADLINE}](2026-10-14.md)" in index and "min read" in index
    assert seen_rows(db) == 8  # every item of the day, cited or not
    shown = capsys.readouterr().out.splitlines()
    assert shown[0].startswith("Saved ") and shown[0].endswith("2026-10-14.md")
    assert "review: approved after 1 round(s)" in shown and shown[-1].startswith("usage: 2 call(s)")


def test_the_saved_json_holds_no_item_text_prompts_or_key(monkeypatch, env):
    _, out = env
    script(monkeypatch, [make_draft(300), APPROVE])
    generate(monkeypatch, env)
    text = (out / "2026-10-14.json").read_text()
    assert FAKE_KEY not in text and "<<<ITEM" not in text and "Short teaser" not in text
    assert "You are the" not in text  # no system prompt


def test_a_second_run_for_the_same_date_says_already_exists_and_spends_nothing(monkeypatch, env, capsys):
    db, out = env
    script(monkeypatch, [make_draft(300), APPROVE])
    generate(monkeypatch, env)
    capsys.readouterr()
    before = {name: (out / name).read_bytes() for name in files(out)}
    queue = script(monkeypatch, [make_draft(300), APPROVE])
    assert generate(monkeypatch, env) == 0
    shown = capsys.readouterr().out
    assert shown.strip() == f"ALREADY_EXISTS: {out / '2026-10-14.md'}"
    assert len(queue) == 2  # no model call was made
    assert {name: (out / name).read_bytes() for name in files(out)} == before
    assert seen_rows(db) == 8


def test_already_exists_is_reported_without_a_key_or_settings(monkeypatch, env, capsys):
    _, out = env
    out.mkdir()
    (out / "2026-10-14.md").write_text("done")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert generate(monkeypatch, env) == 0
    assert capsys.readouterr().out.startswith("ALREADY_EXISTS:")


@pytest.mark.parametrize(
    ("replies", "footer"),
    [
        ([make_draft(300), APPROVE], "*Review: approved after 1 round(s).*"),
        ([make_draft(300), blocked(), make_draft(300), blocked(), make_draft(300), blocked()],
         "*Review: not converged after 3 round(s); 1 sentence removed.*"),
        ([(make_draft(300), (30_000, 1_000)), (blocked(), (30_000, 1_000)), (make_draft(300), (30_000, 1_000))],
         "*Review: degraded at 80% of the budget after 1 round(s); 1 sentence removed.*"),
    ],
    ids=["approved", "not_converged", "degraded"],
)
def test_reviewed_digests_are_saved_with_their_review_line(monkeypatch, env, replies, footer):
    db, out = env
    script(monkeypatch, replies)
    assert generate(monkeypatch, env) == 0
    assert (out / "2026-10-14.md").read_text().endswith(f"\n\n{footer}\n")
    assert seen_rows(db) == 8


def test_no_review_saves_nothing_marks_nothing_and_exits_three(monkeypatch, env, capsys):
    db, out = env
    queue = script(monkeypatch, [make_draft(300)])
    assert generate(monkeypatch, env, "--no-review") == 3
    shown = capsys.readouterr().out
    assert "NOT SAVED" in shown and "review: NOT REVIEWED" in shown and shown.splitlines()[-1].startswith("usage: 1 call(s)")
    assert files(out) == [] and seen_rows(db) == 0 and queue == []


def test_a_budget_that_blocks_the_first_review_saves_nothing_and_exits_three(monkeypatch, env):
    from decimal import Decimal

    db, out = env
    settings = cli.load_llm_settings(cli.DEFAULT_SETTINGS_PATH).model_copy(update={"max_usd_per_run": Decimal("0.10")})
    monkeypatch.setattr(cli, "load_llm_settings", lambda path: settings)
    script(monkeypatch, [(make_draft(300), (30_000, 3_000))])
    assert generate(monkeypatch, env) == 3
    assert files(out) == [] and seen_rows(db) == 0


def test_a_draft_that_cannot_be_repaired_saves_nothing_and_exits_one(monkeypatch, env, capsys):
    db, out = env
    script(monkeypatch, [make_draft(205), blocked(), make_draft(205), blocked(), make_draft(205), blocked()])
    assert generate(monkeypatch, env) == 1
    assert "review: FAILED" in capsys.readouterr().out
    assert files(out) == [] and seen_rows(db) == 0


def test_a_quiet_day_saves_nothing_marks_nothing_and_exits_zero(monkeypatch, tmp_path, capsys):
    db, out = tmp_path / "digest.db", tmp_path / "newsletters"
    with Store(db) as store:
        for item in editor_items(DAY, "a1", "a2"):
            store.add_if_new(item)
    queue = script(monkeypatch, [make_draft(300)])
    assert generate(monkeypatch, (db, out)) == 0
    assert "No significant developments" in capsys.readouterr().out
    assert files(out) == [] and seen_rows(db) == 0 and len(queue) == 1


def test_draft_still_saves_nothing_and_writes_no_ledger_rows(monkeypatch, env, tmp_path):
    db, out = env
    script(monkeypatch, [make_draft(300), APPROVE])
    monkeypatch.setattr(sys, "argv", ["daily-digest", "draft", "--db", str(db), "--date", DAY.isoformat()])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0 and not out.exists() and seen_rows(db) == 0


# --- dates -----------------------------------------------------------------------------

@pytest.mark.parametrize("offset", [0, 1, 30])
def test_a_date_that_is_not_in_the_past_is_rejected_before_any_call(monkeypatch, env, capsys, offset):
    queue = script(monkeypatch, [make_draft(300)])
    assert generate(monkeypatch, env, day=date.today() + timedelta(days=offset)) == 2
    err = capsys.readouterr().err
    assert err.strip() == "error: --date must be earlier than today" and len(queue) == 1


# --- validation ------------------------------------------------------------------------

def good_state() -> DigestState:
    items = editor_items(DAY, *IDS)
    draft = make_draft(300)
    digest = build_digest(draft, items, DAY, NOW, [], APPROVED)
    return DigestState(target_date=DAY, items=items, sent_items=items, draft=draft, digest=digest, review=APPROVED)


def with_paragraph(digest, text):
    return digest.model_copy(update={"worth_a_closer_look": [*digest.worth_a_closer_look, text]})


def renumbered(digest, numbers):
    sources = [s.model_copy(update={"number": n}) for s, n in zip(digest.sources, numbers)]
    return digest.model_copy(update={"sources": sources})


def with_source(digest, index, **fields):
    sources = list(digest.sources)
    sources[index] = sources[index].model_copy(update=fields)
    return digest.model_copy(update={"sources": sources})


BAD_DIGESTS = {
    "marker with no source": (lambda d: with_paragraph(d, "A claim[9]."), "a citation marker has no Sources entry"),
    "source with no marker": (
        lambda d: d.model_copy(update={"sources": [*d.sources, CitedSource(
            number=4, title="Extra", source="Example", published=NOW, url="https://news.example.com/a2")]}),
        "a Sources entry has no citation marker"),
    "gap in the numbering": (lambda d: renumbered(d, [1, 2, 4]), "not 1 to N without gaps"),
    "url not among the items": (
        lambda d: with_source(d, 0, url="https://evil.example.com/x"), "not one of the day's items"),
    "html in a paragraph": (lambda d: with_paragraph(d, "<script>alert(1)</script>"), "angle brackets or HTML"),
    "greater-than sign": (lambda d: with_paragraph(d, "Revenue >$3 billion."), "angle brackets or HTML"),
    "link in a paragraph": (lambda d: with_paragraph(d, "See [the report](https://x.example.com)."), "Markdown link or image"),
    "image in a paragraph": (lambda d: with_paragraph(d, "![chart](https://x.example.com/c.png)"), "Markdown link or image"),
    "link in the headline": (
        lambda d: d.model_copy(update={"headline": "Read [this](https://x.example.com) now"}), "Markdown link or image"),
    "multi-line source title": (
        lambda d: with_source(d, 0, title="Line one\nLine two"), "not a single line"),
    "multi-line source name": (lambda d: with_source(d, 1, source="Wire\r\nService"), "not a single line"),
}


@pytest.mark.parametrize("name", BAD_DIGESTS)
def test_the_built_artifact_is_validated_before_anything_is_written(monkeypatch, env, capsys, name):
    db, out = env
    change, reason = BAD_DIGESTS[name]
    state = good_state()
    bad = change(state.digest)
    assert any(reason in p for p in validate_digest(bad, state.items))
    monkeypatch.setattr(cli, "run_digest", lambda *a, **k: state.model_copy(update={"digest": bad}))
    script(monkeypatch, [])
    assert generate(monkeypatch, env) == 1
    captured = capsys.readouterr()
    assert "the digest was not saved:" in captured.err
    for forbidden in ("alert(1)", "evil.example", "Line one", "x.example.com"):
        assert forbidden not in captured.err and forbidden not in captured.out
    assert files(out) == [] and seen_rows(db) == 0


def test_a_good_digest_has_no_validation_problems():
    state = good_state()
    assert validate_digest(state.digest, state.items) == []


def test_only_reviewed_statuses_may_be_saved():
    state = good_state()
    for status in ("unreviewed", "failed"):
        digest = state.digest.model_copy(update={"review": ReviewSummary(status=status)})
        assert validate_digest(digest, state.items) == ["the digest has not been reviewed"]


def test_the_publisher_does_not_recheck_word_count_or_headline_length():
    state = good_state()
    short = state.digest.model_copy(update={"headline": " ".join(["word"] * 40)})
    assert validate_digest(short, state.items) == []  # the Reviewer's job, not the Publisher's


# --- atomicity and races ---------------------------------------------------------------

def test_a_failure_between_the_temporary_write_and_the_move_leaves_nothing(monkeypatch, env, capsys):
    db, out = env
    real_link = os.link

    def failing(src, dst, *args, **kwargs):
        if str(dst).endswith(".md"):
            raise OSError("disk full")
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", failing)
    script(monkeypatch, [make_draft(300), APPROVE])
    assert generate(monkeypatch, env) == 1
    assert "could not be saved" in capsys.readouterr().err
    assert files(out) == [] and seen_rows(db) == 0  # no .md, no .json, no temporary file, no index


def test_a_markdown_that_appears_between_the_precheck_and_the_move_is_never_replaced(monkeypatch, env, capsys):
    db, out = env
    real_link = os.link

    def racing(src, dst, *args, **kwargs):
        if str(dst).endswith(".md"):
            Path(dst).write_text("THE OTHER RUN")  # another run finishes first
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", racing)
    script(monkeypatch, [make_draft(300), APPROVE])
    assert generate(monkeypatch, env) == 0
    shown = capsys.readouterr().out
    assert f"ALREADY_EXISTS: {out / '2026-10-14.md'}" in shown
    assert files(out) == ["2026-10-14.md"] and (out / "2026-10-14.md").read_text() == "THE OTHER RUN"
    assert seen_rows(db) == 0  # the loser records nothing


def test_the_loser_of_a_race_leaves_a_json_it_did_not_create(tmp_path):
    out = tmp_path / "n"
    published = published_for(DAY)
    save_digest(out, published)  # the winner's complete pair
    keep = (out / "2026-10-14.json").read_bytes()
    with pytest.raises(AlreadyExists):
        save_digest(out, published_for(DAY, headline="A different headline"))
    assert (out / "2026-10-14.json").read_bytes() == keep and (out / "2026-10-14.md").exists()
    assert files(out) == ["2026-10-14.json", "2026-10-14.md"]


def test_a_json_left_by_a_run_that_died_is_replaced(monkeypatch, env):
    _, out = env
    out.mkdir()
    (out / "2026-10-14.json").write_text("{partial")
    script(monkeypatch, [make_draft(300), APPROVE])
    assert generate(monkeypatch, env) == 0
    assert PublishedDigest.model_validate_json((out / "2026-10-14.json").read_text()).digest.covered_date == DAY


def test_no_temporary_file_is_left_after_a_save(tmp_path):
    out = tmp_path / "n"
    save_digest(out, published_for(DAY))
    rebuild_index(out)
    assert files(out) == ["2026-10-14.json", "2026-10-14.md", "index.md"]


# --- the index -------------------------------------------------------------------------

def test_the_index_lists_digests_newest_first_and_heals_after_a_deletion(tmp_path):
    out = tmp_path / "n"
    for day in (date(2026, 10, 12), date(2026, 10, 14), date(2026, 10, 13)):
        save_digest(out, published_for(day, headline=f"Headline for {day.day}"))
    assert rebuild_index(out) == []
    lines = [l for l in (out / "index.md").read_text().splitlines() if l.startswith("- ")]
    assert [l.split("**")[1] for l in lines] == ["2026-10-14", "2026-10-13", "2026-10-12"]
    assert "[Headline for 14](2026-10-14.md) · 2 min read" in lines[0]
    (out / "2026-10-13.md").unlink()
    (out / "2026-10-13.json").unlink()
    rebuild_index(out)
    lines = [l for l in (out / "index.md").read_text().splitlines() if l.startswith("- ")]
    assert [l.split("**")[1] for l in lines] == ["2026-10-14", "2026-10-12"]


def test_the_index_skips_a_corrupt_or_orphan_json_with_a_note(tmp_path):
    out = tmp_path / "n"
    save_digest(out, published_for(DAY))
    (out / "2026-10-10.json").write_text("{not json")
    (out / "2026-10-10.md").write_text("x")
    (out / "2026-10-09.json").write_text(published_for(date(2026, 10, 9)).model_dump_json())  # no .md: never landed
    (out / "2026-10-08.json").write_text(published_for(DAY).model_dump_json())  # the date does not match its name
    (out / "2026-10-08.md").write_text("x")
    (out / "notes.json").write_text("{}")
    notes = rebuild_index(out)
    assert sorted(notes) == [
        "index: skipped 2026-10-08.json (its date does not match its name)",
        "index: skipped 2026-10-10.json (unreadable or invalid)",
    ]
    text = (out / "index.md").read_text()
    assert "2026-10-14.md" in text and "2026-10-10" not in text and "2026-10-09" not in text


def test_the_preview_is_the_first_sentence_without_markers_at_most_160_characters(tmp_path):
    out = tmp_path / "n"
    published = published_for(DAY)
    long_first = "Gartner says " + "very " * 60 + "long things[1]. Second sentence."
    digest = published.digest.model_copy(update={"what_you_need_to_know": [long_first]})
    save_digest(out, published.model_copy(update={"digest": digest}))
    rebuild_index(out)
    preview = [l for l in (out / "index.md").read_text().splitlines() if l.startswith("  ")][0].strip()
    assert len(preview) <= 160 and preview.endswith("…") and "[1]" not in preview
    short = published.digest.model_copy(update={"what_you_need_to_know": ["Costs fell[2]. Then more."]})
    save_digest(out, published_for(date(2026, 10, 13)).model_copy(update={"digest": short.model_copy(update={"covered_date": date(2026, 10, 13)})}))
    rebuild_index(out)
    assert "  Costs fell." in (out / "index.md").read_text()


# --- the ledger ------------------------------------------------------------------------

def test_a_ledger_failure_after_saving_keeps_the_files_and_exits_one(monkeypatch, env, capsys):
    db, out = env

    def broken(self, items, digest_date):
        raise sqlite3.OperationalError("disk I/O error with SECRET-ITEM-TEXT")

    monkeypatch.setattr(Store, "mark_seen", broken)
    script(monkeypatch, [make_draft(300), APPROVE])
    assert generate(monkeypatch, env) == 1
    captured = capsys.readouterr()
    assert "the seen-items ledger was not updated" in captured.err and "may repeat these items" in captured.err
    assert "SECRET-ITEM-TEXT" not in captured.err + captured.out
    assert files(out) == ["2026-10-14.json", "2026-10-14.md", "index.md"] and captured.out.splitlines()[-1].startswith("usage:")


# --- logs and housekeeping -------------------------------------------------------------

def test_the_debug_log_carries_no_text_or_key_from_a_generate_run(monkeypatch, env, capfd):
    script(monkeypatch, [make_draft(300), APPROVE])
    assert generate(monkeypatch, env, "--debug") == 0
    err = capfd.readouterr().err
    assert "saved digest files for 2026-10-14" in err
    for forbidden in (FAKE_KEY, HEADLINE, "alpha", "A survey found"):
        assert forbidden not in err


def test_newsletters_are_git_ignored():
    root = Path(__file__).resolve().parents[1]
    assert "newsletters/" in (root / ".gitignore").read_text().splitlines()
