"""Turn a checked Draft into the final digest. Markers and Sources are built here, never by the model."""

import math
import re
from datetime import date, datetime

from .contracts import CandidateItem, CitedSource, Draft, DraftSentence, FinalDigest, ReviewSummary

WORDS_PER_MINUTE = 200
# Only these carry a citation: facts, quotes and attributed views. Trends and transitions do not.
CITED_TYPES = {"factual", "quote", "attributed_opinion"}
_TRAILING_PUNCTUATION = re.compile(r"[.!?]+$")
_CLOSING = ("”", "’", '"', "'", ")", "]", "»")


def sentences(draft: Draft) -> list[DraftSentence]:
    return [*draft.what_you_need_to_know, *draft.worth_a_closer_look]


def word_count(draft: Draft) -> int:
    """Words in both sections; the headline, markers and Sources are not counted."""
    return sum(len(sentence.text.split()) for sentence in sentences(draft))


def _with_markers(text: str, numbers: list[int]) -> str:
    marks = "".join(f"[{n}]" for n in numbers)
    if text.endswith(_CLOSING):
        return f"{text}{marks}"  # outside the quotation marks, so it never reads as part of a quote
    # Otherwise before the full stop, so the marker sits on the claim.
    match = _TRAILING_PUNCTUATION.search(text)
    return f"{text[:match.start()]}{marks}{match.group()}" if match else f"{text}{marks}"


def _paragraphs(section: list[DraftSentence], number_of: dict[str, int]) -> list[str]:
    paragraphs: list[list[str]] = []
    for sentence in section:
        text = sentence.text
        if sentence.type in CITED_TYPES:
            text = _with_markers(text, [number_of[i] for i in sentence.item_ids])
        if not paragraphs or sentence.starts_paragraph:
            paragraphs.append([])
        paragraphs[-1].append(text)
    return [" ".join(parts) for parts in paragraphs]


def build_digest(
    draft: Draft,
    items: list[CandidateItem],
    covered_date: date,
    generated_at: datetime,
    notes: list[str],
    review: ReviewSummary | None = None,
) -> FinalDigest:
    """Markers are numbered by first appearance; Sources lists exactly the items that got a marker."""
    by_id = {item.id: item for item in items}
    number_of: dict[str, int] = {}
    for sentence in sentences(draft):
        if sentence.type in CITED_TYPES:
            for item_id in sentence.item_ids:
                number_of.setdefault(item_id, len(number_of) + 1)
    sources = [
        CitedSource(
            number=number, title=by_id[item_id].title, source=by_id[item_id].source,
            published=by_id[item_id].published, url=by_id[item_id].canonical_url,
        )
        for item_id, number in number_of.items()
    ]
    words = word_count(draft)
    return FinalDigest(
        covered_date=covered_date,
        generated_at=generated_at,
        headline=draft.headline,
        what_you_need_to_know=_paragraphs(draft.what_you_need_to_know, number_of),
        worth_a_closer_look=_paragraphs(draft.worth_a_closer_look, number_of),
        sources=sources,
        word_count=words,
        reading_time_minutes=max(1, math.ceil(words / WORDS_PER_MINUTE)),
        notes=notes,
        review=review or ReviewSummary(status="unreviewed"),
    )


def link_text(text: str) -> str:
    """Titles come from the web: one line, no brackets, no Markdown emphasis or HTML."""
    text = " ".join(text.split()).replace("[", "(").replace("]", ")")
    return re.sub(r"([\\*_`<>])", r"\\\1", text)


def _link_target(url: str) -> str:
    for char, code in (("(", "%28"), (")", "%29"), (" ", "%20"), ("<", "%3C"), (">", "%3E"), ("`", "%60"), ("\\", "%5C")):
        url = url.replace(char, code)
    return url


def review_text(review: ReviewSummary) -> str:
    """How the digest was reviewed, in the words the `review:` line and the saved file share."""
    rounds = f"{review.rounds} round(s)"
    removed = ""
    if review.removed:
        removed = f"; {review.removed} sentence{'' if review.removed == 1 else 's'} removed"
    return {
        "approved": f"approved after {rounds}",
        "not_converged": f"not converged after {rounds}{removed}",
        "degraded": f"degraded at 80% of the budget after {rounds}{removed}",
        "unreviewed": "NOT REVIEWED",
        "failed": f"FAILED after {rounds}; the draft could not be repaired",
    }[review.status]


def render_markdown(digest: FinalDigest) -> str:
    """The PRD 4.0 shape: headline, dates, reading time, two sections, numbered Sources."""
    generated = digest.generated_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        f"# {digest.headline}",
        "",
        f"Covered {digest.covered_date.isoformat()} · generated {generated} · "
        f"{digest.reading_time_minutes} min read",
        "",
        "## What you need to know",
        "",
        *[line for paragraph in digest.what_you_need_to_know for line in (paragraph, "")],
        "## Worth a closer look",
        "",
        *[line for paragraph in digest.worth_a_closer_look for line in (paragraph, "")],
        "## Sources",
        "",
    ]
    for source in digest.sources:
        lines.append(
            f"{source.number}. [{link_text(source.title)}]({_link_target(source.url)})"
            f" — {link_text(source.source)}, {source.published.date().isoformat()}"
        )
    return "\n".join(lines)
