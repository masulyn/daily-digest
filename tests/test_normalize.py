from daily_digest.normalize import (
    MAX_EXTRACT_CHARS,
    canonicalize_url,
    content_hash,
    make_extract,
    needs_extraction,
    strip_html,
)

import pytest



def test_canonicalize_removes_tracking_fragment_and_trailing_slash():
    url = "https://Example.com/news/story/?utm_source=rss&utm_medium=feed&id=7#comments"
    assert canonicalize_url(url) == "https://example.com/news/story?id=7"


def test_canonicalize_keeps_root_path():
    assert canonicalize_url("https://example.com/") == "https://example.com/"


def test_strip_html_drops_tags_and_scripts():
    html = "<p>Hello <b>world</b></p><script>alert(1)</script><style>p{}</style><p>Again</p>"
    assert strip_html(html) == "Hello world Again"


def test_strip_html_unescapes_entities_and_collapses_whitespace():
    assert strip_html("<p>Fish &amp;   chips</p>\n\n<p>ok</p>") == "Fish & chips ok"


def test_make_extract_truncates_on_word_boundary():
    out = make_extract(("word " * 600).strip())
    assert len(out) <= MAX_EXTRACT_CHARS
    assert out.endswith("word…")


def test_make_extract_respects_the_limit_without_word_boundaries():
    out = make_extract("あ" * 3000)
    assert len(out) == MAX_EXTRACT_CHARS
    assert out.endswith("…")


def test_make_extract_leaves_short_text_alone():
    assert make_extract("short text") == "short text"


def test_content_hash_ignores_case_and_spacing():
    assert content_hash("A  Title", "Some text") == content_hash("a title", "some   TEXT")
    assert content_hash("A title", "x") != content_hash("A title", "y")


LONG_TEXT = "This sentence is part of a complete feed article. " * 12  # about 600 characters


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "A short snippet from a headline-only feed.",
        LONG_TEXT + "...",
        LONG_TEXT + "…",
        LONG_TEXT + "[…]",
        LONG_TEXT + "[...]",
        LONG_TEXT + " Read more",
        LONG_TEXT + " Continue reading →",
        LONG_TEXT + " The post Some Title appeared first on Some Site.",
    ],
)
def test_short_or_teaser_feed_text_needs_extraction(text):
    assert needs_extraction(text)


@pytest.mark.parametrize(
    "text",
    [
        LONG_TEXT,
        LONG_TEXT + " It ended normally.",
        "Starts with... an ellipsis in the middle. " + LONG_TEXT,
        "Readers can read more about it later in the year. " + LONG_TEXT,
    ],
)
def test_complete_feed_text_does_not_need_extraction(text):
    assert not needs_extraction(text)


def _body_of(length: int) -> str:
    return ("Complete sentence here. " * (length // 24 + 1))[: length - 1] + "."


def test_body_longer_than_the_extract_limit_needs_extraction():
    assert not needs_extraction(_body_of(MAX_EXTRACT_CHARS))  # not cut, nothing lost
    assert needs_extraction(_body_of(MAX_EXTRACT_CHARS + 1))
    assert needs_extraction(_body_of(5000))
