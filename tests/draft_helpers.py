"""Builders for drafts with an exact word count."""

from daily_digest.contracts import Draft, DraftSentence
from daily_digest.digest import word_count

QUOTE = "We moved 40 pilots into production this year."
HEADLINE = "Pilots reach production as governance lags"


def sentence(kind: str, ids=("a1",), text: str | None = None, **fields) -> DraftSentence:
    return DraftSentence(text=text or "Alpha beta gamma delta.", type=kind, item_ids=list(ids), **fields)


def filler(words: int, ids=("a3",)) -> DraftSentence:
    text = " ".join(["alpha"] * (words - 1) + ["omega."])
    return DraftSentence(text=text, type="factual", item_ids=list(ids))


def fixed_sentences() -> tuple[list[DraftSentence], list[DraftSentence]]:
    first = [
        DraftSentence(text="Several firms are moving pilots into production.", type="trend",
                      item_ids=["a1", "a2"], starts_paragraph=True),
        DraftSentence(text="A survey found that most firms have no governance owner.", type="factual",
                      item_ids=["a3"]),
    ]
    second = [
        DraftSentence(text=f"Dana Reyes of the bank said, “{QUOTE}”", type="quote", item_ids=["a1"],
                      quote=QUOTE, speaker="Dana Reyes", starts_paragraph=True),
        DraftSentence(text="Lee Park said customers asked for predictable costs.", type="attributed_opinion",
                      item_ids=["a4"], speaker="Lee Park", starts_paragraph=True),
    ]
    return first, second


def make_draft(total_words: int, headline: str = HEADLINE) -> Draft:
    """A draft that passes every check except possibly the length, with `total_words` words."""
    first, second = fixed_sentences()
    used = word_count(Draft(headline=headline, what_you_need_to_know=first, worth_a_closer_look=second))
    return Draft(
        headline=headline,
        what_you_need_to_know=[*first, filler(total_words - used)],
        worth_a_closer_look=second,
    )
