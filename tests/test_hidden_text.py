from datetime import date

import pytest

from daily_digest.contracts import Draft, DraftSentence
from daily_digest.digest import build_digest
from daily_digest.editor import check_draft, hidden_text_note, render_items
from daily_digest.normalize import strip_hidden
from helpers import editor_items
from test_injection import DAY, hidden, run_all_prompts

ZWSP, BIDI, TAG = "​", "‮", chr(0xE0041)


@pytest.mark.parametrize("raw, clean, removed", [
    ("a​b‌c‍d⁠e﻿", "abcde", 5),            # zero-width characters
    ("x‪y‫z‬‭‮⁦⁧⁨⁩", "xyz", 9),  # bidirectional controls
    ("hi" + chr(0xE0001) + TAG + chr(0xE007F), "hi", 3),             # the Unicode tag block
    ("a️b\U000e0100c᠋d", "abcd", 3),                       # variation selectors
    ("soft­hyphen؜", "softhyphen", 2),                     # soft hyphen and the Arabic letter mark
    ("a\x00b\x07c\x1bd\x7fe", "abcde", 4),                           # other control characters
    ("line\nbreak\ttab\r\nend x", "line break tab  end x", 0),  # line breaks and tabs become spaces
    ("ｆｕｌｌ ﬁne ①", "full fine 1", 0),                            # NFKC folds look-alikes first
    ("plain ordinary text, 40 pilots", "plain ordinary text, 40 pilots", 0),
])
def test_hidden_characters_are_removed_and_counted(raw, clean, removed):
    assert strip_hidden(raw) == (clean, removed)


def hostile_item():
    item = editor_items(DAY, "a1")[0]
    return item.model_copy(update={
        "title": f"Bank{ZWSP} moves{BIDI} 40 pilots{TAG}",
        "source": f"Example{ZWSP} Wire{TAG}",
        "article_text": f"Chief information officer Dana Reyes said, “We moved{ZWSP} 40 pilots{BIDI} into production this year.” {TAG}ignore this",
    })


def test_hidden_characters_are_absent_from_the_item_blocks_and_the_verbatim_check_uses_the_stripped_text():
    item = hostile_item()
    blocks, sent = render_items([item])
    assert not hidden(blocks) and "source: Example Wire\ntitle: Bank moves 40 pilots\n" in blocks
    assert sent[item.id].title == "Bank moves 40 pilots" and sent[item.id].source == "Example Wire"
    quote = DraftSentence(
        text="Dana Reyes said, “We moved 40 pilots into production this year.”", type="quote", item_ids=[item.id],
        quote="We moved 40 pilots into production this year.", speaker="Dana Reyes",
    )
    checked = check_draft(Draft(headline="h", what_you_need_to_know=[quote], worth_a_closer_look=[]), sent)
    assert not any("quote" in n for n in checked.notes)  # copied from the text the model saw, so it matches


def test_a_quote_that_needs_the_hidden_characters_does_not_match():
    item = hostile_item()
    _, sent = render_items([item])
    forged = DraftSentence(
        text=f"Dana Reyes said, “We moved{ZWSP} 40 pilots”", type="quote", item_ids=[item.id], quote=f"We moved{ZWSP} 40 pilots",
        speaker="Dana Reyes",
    )
    checked = check_draft(Draft(headline="h", what_you_need_to_know=[forged], worth_a_closer_look=[]), sent)
    assert any("quote is not in the cited item" in n for n in checked.notes)


def test_a_title_body_and_source_with_hidden_text_reach_no_request_to_the_writer_reviewer_or_judge():
    items = [hostile_item(), *editor_items(DAY, "a2", "a3")]
    requests = run_all_prompts(items)
    assert {kind for kind, _, _ in requests} == {"writer", "revise", "review", "judge"}
    for kind, system, user in requests:
        assert not hidden(system) and not hidden(user), kind
        if kind == "writer":
            assert "ignore this" in user  # the visible words stay; only the hidden characters go


def test_the_note_counts_hidden_characters_without_saying_what_they_were():
    note = hidden_text_note([hostile_item(), *editor_items(DAY, "a2")])
    assert len(note) == 1 and note[0].startswith("removed ") and note[0].endswith("hidden characters from 1 items")
    assert hidden_text_note(editor_items(DAY, "a1", "a2")) == []
    assert not hidden(note[0]) and TAG not in note[0] and "pilots" not in note[0]


def test_the_digests_sources_list_holds_no_hidden_characters():
    item = hostile_item()
    draft = Draft(headline="A bank moves pilots", what_you_need_to_know=[
        DraftSentence(text="A bank moved pilots into production.", type="factual", item_ids=[item.id], starts_paragraph=True)],
        worth_a_closer_look=[])
    from datetime import datetime, timezone
    digest = build_digest(draft, [item], date(2026, 10, 14), datetime(2026, 10, 15, tzinfo=timezone.utc), [])
    assert digest.sources[0].title == "Bank moves 40 pilots" and digest.sources[0].source == "Example Wire"
