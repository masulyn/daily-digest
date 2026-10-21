"""Validate, save and index a finished digest.

The Reviewer already checked the draft's words. This module checks only what code built from the
draft (the Sources list, the markers, the rendered text) and whether the status allows saving. Files
are written whole or not at all, and the Markdown file is moved into place last: it is what makes a
digest exist.
"""

import logging
import os
import re
import tempfile
from pathlib import Path

from .contracts import CandidateItem, Draft, FinalDigest, ProvenanceEntry, PublishedDigest
from .digest import link_text, render_markdown, review_text

log = logging.getLogger(__name__)

SAVEABLE_STATUSES = ("approved", "not_converged", "degraded")
INDEX_NAME = "index.md"
PREVIEW_CHARS = 160

_MARKER = re.compile(r"\[(\d+)\]")
# Inline links and images (`](`), reference links (`][x`, but not stacked markers like `[1][2]`) and
# reference definitions (`[x]: dest` opening a line). Plain brackets, as in a quote's "[T]he", are fine.
_LINK_OR_IMAGE = re.compile(r"\]\(|\]\[(?!\d+\])|^\s*\[[^\]\n]*\]:", re.M)
_DIGEST_JSON = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")


class AlreadyExists(Exception):
    """A finished digest for this date is already in place."""

    def __init__(self, path: Path) -> None:
        super().__init__(str(path))
        self.path = path


def digest_path(out_dir: Path, covered) -> Path:
    return out_dir / f"{covered.isoformat()}.md"


def validate_digest(digest: FinalDigest, items: list[CandidateItem]) -> list[str]:
    """Reasons the built digest must not be saved. Fixed wording: never any model or item text."""
    problems = []
    if digest.review.status not in SAVEABLE_STATUSES:
        problems.append("the digest has not been reviewed")
    paragraphs = [*digest.what_you_need_to_know, *digest.worth_a_closer_look]
    marked = {int(n) for paragraph in paragraphs for n in _MARKER.findall(paragraph)}
    numbers = [source.number for source in digest.sources]
    if numbers != list(range(1, len(numbers) + 1)):
        problems.append("the Sources numbers are not 1 to N without gaps")
    if marked - set(numbers):
        problems.append("a citation marker has no Sources entry")
    if set(numbers) - marked:
        problems.append("a Sources entry has no citation marker")
    known_urls = {item.canonical_url for item in items}
    if any(source.url not in known_urls for source in digest.sources):
        problems.append("a Sources URL is not one of the day's items")
    if not all(source.url.startswith(("http://", "https://")) for source in digest.sources):
        problems.append("a Sources URL is not an http or https link")
    one_line = lambda text: len(text.splitlines()) == 1  # noqa: E731 - also rejects the empty string
    if not all(one_line(source.title) and one_line(source.source) for source in digest.sources):
        problems.append("a Sources title or name is not a single line")
    body = [digest.headline, *paragraphs]
    if any("<" in text or ">" in text for text in body):
        problems.append("the text contains angle brackets or HTML")
    if any(_LINK_OR_IMAGE.search(text) for text in body):
        problems.append("the text contains a Markdown link or image")
    return problems


def provenance_for(draft: Draft) -> list[ProvenanceEntry]:
    return [
        ProvenanceEntry(
            section=section, index=index, type=s.type, item_ids=s.item_ids, quote=s.quote, speaker=s.speaker
        )
        for section in ("what_you_need_to_know", "worth_a_closer_look")
        for index, s in enumerate(getattr(draft, section))
    ]


def markdown_for(digest: FinalDigest) -> str:
    """Exactly what `draft` prints, plus one italic line saying how the digest was reviewed."""
    return f"{render_markdown(digest)}\n\n*Review: {review_text(digest.review)}.*\n"


def _write_temp(directory: Path, name: str, text: str) -> Path:
    """A complete, fsynced temporary file beside the target, hidden and never matching a digest name."""
    descriptor, temp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=directory)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, 0o666 & ~_umask())
    return Path(temp)


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _place_json(temp: Path, target: Path, markdown: Path) -> str:
    """Put the JSON in place: "linked" if this run created the file, "took_over" if it replaced a stale one."""
    try:
        os.link(temp, target)
        return "linked"
    except FileExistsError:
        if markdown.exists():
            raise AlreadyExists(markdown) from None
        # A JSON with no Markdown beside it is left over from a run that died before finishing.
        os.replace(temp, target)
        return "took_over"


def _is_same_file(path: Path, expected: os.stat_result) -> bool:
    try:
        found = os.stat(path)
    except OSError:
        return False
    return (found.st_dev, found.st_ino) == (expected.st_dev, expected.st_ino)


def save_digest(out_dir: Path, published: PublishedDigest) -> Path:
    """Write the JSON and then the Markdown, never replacing an existing Markdown.

    Raises AlreadyExists when a digest for the date is there, including one that appeared while this
    run was working. Whatever happens, no partial digest and no temporary file is left behind. A run
    that fails removes the JSON only if it created that file and it is still the very file it wrote:
    another run may have replaced it, and the winner's JSON must survive.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = published.digest.covered_date.isoformat()
    markdown, json_file = out_dir / f"{stem}.md", out_dir / f"{stem}.json"
    temps: list[Path] = []
    created: os.stat_result | None = None  # the JSON as this run linked it, if this run created it

    def remove_own_json() -> None:
        if created is not None and _is_same_file(json_file, created):
            json_file.unlink(missing_ok=True)

    try:
        json_temp = _write_temp(out_dir, json_file.name, published.model_dump_json(indent=2) + "\n")
        temps.append(json_temp)
        markdown_temp = _write_temp(out_dir, markdown.name, markdown_for(published.digest))
        temps.append(markdown_temp)
        written = os.stat(json_temp)
        if _place_json(json_temp, json_file, markdown) == "linked":
            created = written
        _fsync_directory(out_dir)  # the JSON is on disk before the Markdown makes the digest exist
        os.link(markdown_temp, markdown)  # fails if the file exists, so two runs cannot replace each other
    except (AlreadyExists, FileExistsError):
        remove_own_json()
        raise AlreadyExists(markdown) from None
    except BaseException:
        remove_own_json()
        raise
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)
    _fsync_directory(out_dir)
    log.debug("saved digest files for %s", stem)
    return markdown


def _preview(digest: FinalDigest) -> str:
    if not digest.what_you_need_to_know:
        return ""
    text = " ".join(_MARKER.sub("", digest.what_you_need_to_know[0]).split())
    text = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    text = link_text(text)  # escaping adds characters, so the limit is applied to the escaped text
    if len(text) <= PREVIEW_CHARS:
        return text
    cut = text[: PREVIEW_CHARS - 1].rstrip()
    if (len(cut) - len(cut.rstrip("\\"))) % 2:
        cut = cut[:-1]  # never leave a lone backslash that would escape the ellipsis
    return cut + "…"


def rebuild_index(out_dir: Path) -> list[str]:
    """Rewrite `index.md` from the JSON files, newest first. Returns notes about files it skipped."""
    notes: list[str] = []
    digests: list[FinalDigest] = []
    for path in sorted(out_dir.iterdir()):
        if not _DIGEST_JSON.match(path.name) or not path.with_suffix(".md").exists():
            continue  # not a digest file, or a JSON whose Markdown never landed
        try:
            digest = PublishedDigest.model_validate_json(path.read_text(encoding="utf-8")).digest
        except (OSError, ValueError):
            notes.append(f"index: skipped {path.name} (unreadable or invalid)")
            continue
        if digest.covered_date.isoformat() != path.stem:
            notes.append(f"index: skipped {path.name} (its date does not match its name)")
            continue
        digests.append(digest)
    digests.sort(key=lambda d: d.covered_date, reverse=True)
    lines = ["# Daily Digest", "", "Newest first.", ""]
    for digest in digests:
        stem = digest.covered_date.isoformat()
        headline = link_text(" ".join(digest.headline.split()))
        lines.append(f"- **{stem}** · [{headline}]({stem}.md) · {digest.reading_time_minutes} min read")
        if preview := _preview(digest):
            lines.append(f"  {preview}")
    temp = _write_temp(out_dir, INDEX_NAME, "\n".join(lines) + "\n")
    try:
        os.replace(temp, out_dir / INDEX_NAME)
    finally:
        temp.unlink(missing_ok=True)
    return notes
