from daily_digest.normalize import (
    MAX_EXTRACT_CHARS,
    canonicalize_url,
    content_hash,
    make_extract,
    strip_html,
)


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
