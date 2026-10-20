import re
from datetime import date, datetime, timezone

from daily_digest.contracts import Draft, DraftSentence
from daily_digest.digest import build_digest, render_markdown
from draft_helpers import HEADLINE, filler, make_draft
from helpers import editor_items

DAY = date(2026, 10, 14)
NOW = datetime(2026, 10, 15, 8, 30, tzinfo=timezone.utc)
ITEMS = editor_items(DAY)


def digest_of(draft: Draft):
    return build_digest(draft, ITEMS, DAY, NOW, [])


def test_markers_only_on_facts_quotes_and_attributed_views():
    digest = digest_of(make_draft(220))
    first, second = digest.what_you_need_to_know, digest.worth_a_closer_look
    assert first[0].startswith("Several firms are moving pilots into production.")
    assert "[" not in first[0].split(" A survey")[0]  # the trend sentence has no marker
    assert "no governance owner[1]." in first[0]
    assert second[0].endswith("this year.”") or "[2]" in second[0]
    text = " ".join(first + second)
    assert "[1]" in text and "[2]" in text and "[3]" in text


def test_sources_are_exactly_the_cited_items_numbered_by_first_appearance():
    digest = digest_of(make_draft(220))
    # a3 is cited first (the trend cites a1 and a2, which get no marker), then a1, then a4
    assert [(s.number, s.url) for s in digest.sources] == [
        (1, "https://news.example.com/a3"),
        (2, "https://news.example.com/a1"),
        (3, "https://news.example.com/a4"),
    ]
    markers = {int(n) for n in re.findall(r"\[(\d+)\]", " ".join(digest.what_you_need_to_know + digest.worth_a_closer_look))}
    assert markers == {s.number for s in digest.sources}


def test_marker_sits_before_the_closing_punctuation_and_stacks_for_several_items():
    draft = Draft(
        headline=HEADLINE,
        what_you_need_to_know=[DraftSentence(text="Costs fell.", type="factual", item_ids=["a5", "a6"])],
        worth_a_closer_look=[DraftSentence(text="No stop", type="factual", item_ids=["a7"])],
    )
    digest = digest_of(draft)
    assert digest.what_you_need_to_know == ["Costs fell[1][2]."]
    assert digest.worth_a_closer_look == ["No stop[3]"]


def test_sentences_group_into_paragraphs_where_the_model_started_them():
    draft = make_draft(220)
    digest = digest_of(draft)
    assert len(digest.what_you_need_to_know) == 1  # only the first sentence starts a paragraph
    assert len(digest.worth_a_closer_look) == 2


def test_word_count_and_reading_time():
    assert digest_of(make_draft(200)).reading_time_minutes == 1
    assert digest_of(make_draft(201)).reading_time_minutes == 2
    assert digest_of(make_draft(500)).word_count == 500


def test_markdown_has_the_prd_shape_and_only_stored_urls():
    digest = digest_of(make_draft(220))
    markdown = render_markdown(digest)
    lines = markdown.splitlines()
    assert lines[0] == f"# {HEADLINE}"
    assert "Covered 2026-10-14" in lines[2] and "min read" in lines[2]
    headings = [line for line in lines if line.startswith("## ")]
    assert headings == ["## What you need to know", "## Worth a closer look", "## Sources"]
    assert [line[:4] for line in lines[-3:]] == ["1. [", "2. [", "3. ["]
    stored = {item.canonical_url for item in ITEMS}
    assert set(re.findall(r"https?://[^)\s]+", markdown)) <= stored


def test_titles_cannot_break_the_link_syntax():
    items = editor_items(DAY, "a1")
    items[0] = items[0].model_copy(update={
        "title": "A [tricky] title", "canonical_url": "https://news.example.com/a(1)",
    })
    draft = Draft(headline=HEADLINE, what_you_need_to_know=[filler(10, ("a1",))],
                  worth_a_closer_look=[filler(10, ("a1",))])
    markdown = render_markdown(build_digest(draft, items, DAY, NOW, []))
    assert "[A (tricky) title](https://news.example.com/a%281%29)" in markdown
