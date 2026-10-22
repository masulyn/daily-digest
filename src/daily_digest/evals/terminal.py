"""Terminal-safe text for eval output, reusing the sanitising the `draft` command prints with."""

from ..contracts import ReviewSummary


def printable(text: str) -> str:
    from ..cli import _printable  # imported late: cli imports the eval commands only when `eval` runs

    return _printable(text)


def review_lines(review: ReviewSummary) -> list[str]:
    from ..cli import _review_lines

    return _review_lines(review)
