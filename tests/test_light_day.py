"""Light days: with few items the digest may be shorter than 200 words, flagged, instead of padded."""

import json
import re
from datetime import date, datetime, timezone

import pytest

from daily_digest.config import load_llm_settings
from daily_digest.contracts import PublishedDigest, ReviewIssue, ReviewResult, UsageRecord
from daily_digest.digest import build_digest, render_markdown
from daily_digest.editor import (
    AIM_BODY_WORDS,
    LIGHT_REVISE_PROMPT,
    LIGHT_SYSTEM_PROMPT,
    MAX_BODY_WORDS,
    REVISE_PROMPT,
    SYSTEM_PROMPT,
    WriterFailed,
    is_light_day,
    aim_range,
    render_items,
    revise,
    write_draft,
)
from daily_digest.graph import run_digest
from daily_digest.llm import MeteredClient
from daily_digest.llm.mock import MockClient
from daily_digest.publisher import markdown_for, provenance_for, rebuild_index, save_digest
from daily_digest.reviewer import mechanical_issues, run_review
from daily_digest.store import Store
from draft_helpers import make_draft
from helpers import editor_items

DAY = date(2026, 10, 14)
NOW = datetime(2026, 10, 15, 8, tzinfo=timezone.utc)
APPROVE = ReviewResult(verdict="approve")


def items(count: int):
    return editor_items(DAY, *[f"a{n}" for n in range(1, count + 1)])


def written(count: int, words: int):
    """The draft write_draft returns for `count` items when the model replies with a `words`-word draft."""
    return write_draft(items(count), MockClient([make_draft(words), make_draft(words)]), fit=False)[0]


# --- what makes a day light ------------------------------------------------------------

@pytest.mark.parametrize("count, light", [(2, False), (3, True), (4, True), (7, True), (8, False), (9, False)])
def test_a_day_is_light_with_at_least_three_and_fewer_than_the_setting(count, light):
    assert is_light_day(count, 8) is light


# --- the Writer's word floor -----------------------------------------------------------

def test_a_day_with_four_items_accepts_50_words_and_rejects_49():
    assert written(4, 50)
    with pytest.raises(WriterFailed, match="the sections have 49 words; they must have 50 to 500"):
        written(4, 49)


def test_a_day_with_eight_items_still_needs_200_words():
    assert written(8, 200)
    with pytest.raises(WriterFailed, match="have 150 words; they must have 200 to 500"):
        written(8, 150)
    with pytest.raises(WriterFailed, match="have 199 words; they must have 200 to 500"):
        written(8, 199)


@pytest.mark.parametrize("count", [4, 7, 8, 11])  # the fixed draft cites a1 to a4
def test_500_words_is_the_ceiling_on_every_day(count):
    assert written(count, 500)
    with pytest.raises(WriterFailed, match="have 501 words; they must have .* to 500"):
        written(count, 501)


def test_the_floor_follows_the_setting():
    assert write_draft(items(4), MockClient([make_draft(45)], low_volume_min_words=45), fit=False)[0]
    with pytest.raises(WriterFailed, match="44 words; they must have 45 to 500"):
        write_draft(items(4), MockClient([make_draft(44)] * 2, low_volume_min_words=45), fit=False)


def test_revise_uses_the_same_floor_on_a_light_day_and_the_normal_floor_otherwise():
    first = make_draft(300)
    issue = []
    assert revise(first, issue, items(4), MockClient([make_draft(50)]))
    with pytest.raises(WriterFailed, match="49 words; they must have 50 to 500"):
        revise(first, issue, items(4), MockClient([make_draft(49), make_draft(49)]))
    with pytest.raises(WriterFailed, match="150 words; they must have 200 to 500"):
        revise(first, issue, items(8), MockClient([make_draft(150), make_draft(150)]))


# --- the Reviewer's mechanical layer ---------------------------------------------------

def sent_for(count: int):
    return render_items(items(count))[1]


def length_issues(draft, count, floor):
    return [i for i in mechanical_issues(draft, sent_for(count), floor) if "words" in i.required_change]


def test_the_reviewer_raises_no_length_issue_for_a_valid_light_day_digest_and_one_below_the_floor():
    assert length_issues(make_draft(50), 4, 50) == []
    below = length_issues(make_draft(49), 4, 50)
    assert len(below) == 1 and below[0].section == "headline" and "they must have 50 to 500" in below[0].required_change
    assert length_issues(make_draft(150), 8, 200)  # a normal day is still held to 200


def test_run_review_approves_a_short_digest_on_a_light_day_and_blocks_it_on_a_normal_day():
    light = run_review(make_draft(120), items(4), MockClient([APPROVE]))
    assert light.summary.status == "approved" and light.draft is not None
    normal = run_review(make_draft(120), items(8), MockClient([APPROVE], max_review_rounds=1))
    assert normal.summary.status == "failed" and normal.draft is None  # a model "approve" does not hide the length issue
    assert "could not repair the draft: the sections have 120 words; they must have 200 to 500" in normal.notes


def test_the_fallback_uses_the_light_day_floor_too():
    flagged = ReviewResult(issues=[ReviewIssue(
        section="what_you_need_to_know", index=1, severity="blocking", category="unsupported", required_change="Cut it.")])
    # removing one 14-word sentence from a 120-word draft leaves 106 words: fine on a light day, short on a normal one
    outcome = run_review(make_draft(120), items(4), MockClient([flagged, make_draft(120), flagged, make_draft(120), flagged]))
    assert outcome.summary.status == "not_converged" and outcome.draft is not None
    outcome = run_review(make_draft(205), items(8), MockClient([flagged, make_draft(205), flagged, make_draft(205), flagged]))
    assert outcome.summary.status == "failed"


# --- prompts ---------------------------------------------------------------------------

def test_the_light_day_prompts_give_the_aim_range_and_coverage_wording_and_never_the_floor():
    for prompt in (LIGHT_SYSTEM_PROMPT, LIGHT_REVISE_PROMPT):
        assert "light day" in prompt and "80 to 120 words" in prompt
        assert "Give each relevant item one or two sentences. State every fact once; never repeat or restate a fact." in prompt
        assert "at most 500 words" in prompt and not re.search(r"\b50\b", prompt)  # the floor must not become the target
        assert "short digest is expected" not in prompt and "filler" not in prompt
    assert aim_range(100) == "80 to 120" and aim_range(60) == "48 to 72"
    assert AIM_BODY_WORDS not in LIGHT_SYSTEM_PROMPT and "minimum" not in LIGHT_SYSTEM_PROMPT


def test_the_light_day_prompt_follows_the_aim_setting():
    from daily_digest.editor import writer_prompt

    assert "aim for 120 to 180 words" in writer_prompt(150)


def test_the_normal_day_prompts_are_unchanged_by_the_light_day_rules():
    for prompt in (SYSTEM_PROMPT, REVISE_PROMPT):
        assert "light day" not in prompt and "State every fact once" not in prompt
        assert f"The two sections together are 200 to {MAX_BODY_WORDS} words" in prompt
    assert f"aim for {AIM_BODY_WORDS} words" in SYSTEM_PROMPT


def test_the_writer_is_sent_the_prompt_for_the_day():
    for count, expected in ((4, LIGHT_SYSTEM_PROMPT), (8, SYSTEM_PROMPT)):
        client = MockClient([make_draft(300)])
        write_draft(items(count), client, fit=False)
        assert client.requests[0]["system"] == expected
    light_revise = MockClient([make_draft(300)])
    revise(make_draft(300), [], items(4), light_revise)
    assert light_revise.requests[0]["system"] == LIGHT_REVISE_PROMPT


# --- the flag in the digest, the Markdown and the saved JSON ---------------------------

def pipeline(count: int, words: int):
    store = Store(":memory:")
    for item in items(count):
        store.add_if_new(item)
        if item.article_text:
            store.save_article_text(item.id, item.article_text)
    return run_digest(store, DAY, MockClient([make_draft(words), APPROVE]), review_enabled=True)


def test_a_light_day_digest_carries_the_flag_the_count_and_one_italic_line_under_the_date_line():
    state = pipeline(4, 120)
    assert state.digest.light_day and state.digest.item_count == 4
    lines = render_markdown(state.digest).splitlines()
    assert lines[2].startswith("Covered ") and lines[3] == "" and lines[4] == "*Light day: 4 items today.*" and lines[5] == ""
    assert lines[6] == "## What you need to know"


def test_the_saved_light_day_files_show_the_flag_and_a_normal_day_shows_neither():
    for count, words, light in ((4, 120, True), (8, 300, False)):
        digest = pipeline(count, words).digest
        published = PublishedDigest(
            digest=digest, provenance=provenance_for(make_draft(words)),
            usage=UsageRecord(model="m", calls=2, input_tokens=1, output_tokens=1, usd="0.01"),
        )
        saved = json.loads(published.model_dump_json())["digest"]
        markdown = markdown_for(digest)
        if light:
            assert saved["light_day"] is True and saved["item_count"] == 4 and "*Light day: 4 items today.*" in markdown
        else:
            assert "light_day" not in saved and "item_count" not in saved and "Light day" not in markdown
        assert PublishedDigest.model_validate_json(published.model_dump_json()).digest.light_day is light


def test_a_digest_saved_before_the_flag_existed_still_loads():
    digest = pipeline(8, 300).digest
    old = json.loads(digest.model_dump_json())
    assert "light_day" not in old
    assert type(digest).model_validate(old).light_day is False


def test_the_index_preview_ignores_the_light_day_line(tmp_path):
    digest = pipeline(4, 120).digest
    published = PublishedDigest(
        digest=digest, provenance=provenance_for(make_draft(120)),
        usage=UsageRecord(model="m", calls=2, input_tokens=1, output_tokens=1, usd="0.01"),
    )
    save_digest(tmp_path, published)
    rebuild_index(tmp_path)
    index = (tmp_path / "index.md").read_text()
    assert "Light day" not in index and digest.headline in index


def test_the_real_settings_have_the_documented_defaults_and_reach_the_client():
    settings = load_llm_settings()
    client = MeteredClient(MockClient([]), settings)
    assert (client.low_volume_max_items, client.low_volume_min_words, client.low_volume_aim_words) == (8, 50, 100)
