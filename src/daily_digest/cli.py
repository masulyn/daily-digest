"""Command-line entry point."""

import argparse
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import SecretStr

from .config import (
    DEFAULT_FEEDS_PATH,
    DEFAULT_NEWSLETTERS_DIR,
    DEFAULT_SETTINGS_PATH,
    LLMSettings,
    SettingsError,
    data_dir,
    load_llm_settings,
    load_sources,
)
from .contracts import PublishedDigest, ReviewSummary, UsageRecord
from .digest import render_markdown, review_text
from .editor import WriterFailed
from .extract import extract_articles
from .fetch import HttpFetcher, user_agent
from .graph import DigestState, day_window, run_digest
from .ingest import ingest
from .llm import LLMClient, LLMError, MeteredClient
from .publisher import AlreadyExists, digest_path, provenance_for, rebuild_index, save_digest, validate_digest
from .robots import RobotsChecker
from .store import DatabaseNotFound, Store


def _warn_if_no_contact() -> None:
    if not os.environ.get("DIGEST_CONTACT"):
        print(
            "warning: DIGEST_CONTACT is not set; site owners cannot reach you from our User-Agent",
            file=sys.stderr,
        )


def _open_existing(db_path: Path) -> Store | None:
    """Open a database that must already exist; on failure print why and return None."""
    try:
        return Store(db_path, must_exist=True)
    except DatabaseNotFound as error:
        print(_printable(str(error)), file=sys.stderr)
        return None


def run_ingest(db_path: Path, feeds_path: Path) -> int:
    _warn_if_no_contact()
    sources = load_sources(feeds_path)
    with HttpFetcher() as fetcher, Store(db_path) as store:
        robots = RobotsChecker(fetcher.fetch, user_agent())
        reports = ingest(sources, fetcher.fetch, robots, store)
        print(f"{'source':34} {'entries':>7} {'stored':>6} {'dupes':>5} {'skipped':>7}  error")
        for r in reports:
            print(f"{r.source:34} {r.entries:7} {r.stored:6} {r.duplicates:5} {r.skipped:7}  {r.error or ''}")
        print(f"\nItems in database: {store.count()}  ({db_path})")
    # Partial failures are reported in the table; only fail when nothing was ingested.
    return 0 if any(r.error is None for r in reports) else 1


DEFAULT_EXTRACT_DAYS = 2  # a digest covers one day, so older pages are never read


def extract_since(today: date, days: int) -> datetime:
    """Local midnight at the start of the day `days - 1` days before `today`."""
    return day_window(today - timedelta(days=days - 1))[0]


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def run_extract(db_path: Path, limit: int | None, days: int = DEFAULT_EXTRACT_DAYS) -> int:
    _warn_if_no_contact()
    store = _open_existing(db_path)
    if store is None:
        return 2
    since = extract_since(date.today(), days)
    with store, HttpFetcher() as fetcher:
        robots = RobotsChecker(fetcher.fetch, user_agent())
        report = extract_articles(
            store, fetcher.fetch, robots, set_host_delay=fetcher.set_host_delay, limit=limit,
            since=since,
        )
    print(f"Window: items published since {since.date().isoformat()} (--days {days}).")
    print(f"Considered {report.considered}, extracted {report.extracted}, deferred {report.deferred}.")
    for code, n in sorted(report.failures.items()):
        print(f"  {code}: {n}")
    # Like ingest: fail only when articles were attempted and none came back.
    return 1 if report.considered and not report.extracted else 0


def _make_inner_client(settings: LLMSettings, api_key: SecretStr) -> LLMClient:
    # Imported here so commands that never call a model don't load the SDK.
    from .llm.anthropic_client import AnthropicClient

    return AnthropicClient(
        api_key=api_key,
        model=settings.model,
        timeout_seconds=settings.request_timeout_seconds,
    )


def _build_client(settings_path: Path) -> MeteredClient | None:
    """The budgeted model client, or None after printing why one cannot be built."""
    try:
        settings = load_llm_settings(settings_path)
    except SettingsError as error:
        print(_printable(f"error: {error}"), file=sys.stderr)
        return None
    if settings.provider != "anthropic":
        print(f"error: provider {settings.provider!r} is only for tests; use --stub", file=sys.stderr)
        return None
    raw_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not raw_key.strip():
        print("error: ANTHROPIC_API_KEY is not set (use --stub to run without a model)", file=sys.stderr)
        return None
    return MeteredClient(_make_inner_client(settings, SecretStr(raw_key)), settings)


def _printable(text: str) -> str:
    """Feed, model and config text must not send control codes to the terminal."""
    return "".join(c if c.isprintable() or c == "\n" else " " for c in text)


def run_draft(
    db_path: Path, target: date, client: MeteredClient | None = None, review_enabled: bool = True
) -> int:
    """Print the day's digest from the local database, or stub summaries without a client.

    Exit codes: 0 reviewed (or nothing to write), 1 error or an unrepairable draft, 2 setup problem,
    3 the digest was not reviewed and must not be published.
    """
    try:
        return _print_draft(db_path, target, client, review_enabled)
    except (LLMError, WriterFailed) as error:
        print(f"error: {_printable(str(error))}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            print(client.usage_line())


def _review_lines(review: ReviewSummary) -> list[str]:
    return [
        f"review: {review_text(review)}",
        *[f"  - unresolved: {t}" for t in review.unresolved],
        *[f"  - advisory: {t}" for t in review.advisory],
    ]


def _print_draft(db_path: Path, target: date, client: MeteredClient | None, review_enabled: bool) -> int:
    store = _open_existing(db_path)
    if store is None:
        return 2
    with store:
        state = run_digest(store, target, client, review_enabled=review_enabled)
    by_id = {item.id: item for item in state.items}
    for summary in state.summaries:
        item = by_id[summary.item_id]
        print(_printable(f"{item.title} ({item.source})"))
        print("\n".join(f"- {_printable(point)}" for point in summary.key_points))
        print(_printable(f"  {summary.source_url}"))
    review = state.review
    if state.digest and review and review.status == "unreviewed":
        print("NOT REVIEWED: this digest has not been checked and must not be published.\n")
    if state.digest:
        print(_printable(render_markdown(state.digest)))
    if review:
        print(_printable("\n" + "\n".join(_review_lines(review))))
    for note in state.notes:
        print(_printable(note))
    if review and review.status == "failed":
        return 1
    return 3 if review and review.status == "unreviewed" else 0


def _print_review(state: DigestState) -> None:
    if state.review:
        print(_printable("\n" + "\n".join(_review_lines(state.review))))
    for note in state.notes:
        print(_printable(note))


def run_generate(
    db_path: Path, out_dir: Path, target: date, client: MeteredClient, review_enabled: bool = True
) -> int:
    """Make the day's digest for real: run the pipeline, then validate, save, index and mark items seen.

    Exit codes: 0 saved, already there, or nothing to write; 1 error, a draft that could not be
    repaired, or a digest that failed validation; 2 setup problem; 3 not reviewed (nothing saved).
    """
    try:
        return _generate(db_path, out_dir, target, client, review_enabled)
    except (LLMError, WriterFailed) as error:
        print(f"error: {_printable(str(error))}", file=sys.stderr)
        return 1
    finally:
        print(client.usage_line())


def _generate(db_path: Path, out_dir: Path, target: date, client: MeteredClient, review_enabled: bool) -> int:
    store = _open_existing(db_path)
    if store is None:
        return 2
    with store:
        state = run_digest(store, target, client, review_enabled=review_enabled)
        digest, review = state.digest, state.review
        if digest is None:  # a quiet day, or a draft that could not be repaired
            _print_review(state)
            return 1 if review and review.status == "failed" else 0
        if review.status == "unreviewed":
            print("NOT SAVED: this digest has not been reviewed and must not be published.\n")
            print(_printable(render_markdown(digest)))
            _print_review(state)
            return 3
        problems = validate_digest(digest, state.items)
        if problems:
            for problem in problems:
                print(f"error: the digest was not saved: {problem}", file=sys.stderr)
            return 1
        published = PublishedDigest(
            digest=digest, provenance=provenance_for(state.draft),
            usage=UsageRecord(
                model=client.model, calls=client.calls, input_tokens=client.input_tokens,
                output_tokens=client.output_tokens, usd=client.spent,
                unknown_usage_attempts=client.unknown_usage_attempts,
            ),
        )
        try:
            path = save_digest(out_dir, published)
        except AlreadyExists as exists:
            print(_printable(f"ALREADY_EXISTS: {exists.path}"))
            return 0
        except OSError as error:
            reason = os.strerror(error.errno) if error.errno else "I/O error"
            print(
                f"error: the digest could not be saved ({reason}); nothing was written and no items were marked seen",
                file=sys.stderr,
            )
            return 1
        index_notes = _refresh_index(out_dir)
        try:
            store.mark_seen(state.items, target)
        except Exception:  # noqa: BLE001 - whatever the database says, the files are already saved
            print(
                _printable(f"error: the digest was saved to {path}, but the seen-items ledger was not "
                           "updated; the next digest may repeat these items"),
                file=sys.stderr,
            )
            return 1
    print(_printable(f"Saved {path}"))
    _print_review(state)
    for note in index_notes:
        print(_printable(note))
    return 0


def run_pipeline(
    db_path: Path, feeds_path: Path, out_dir: Path, target: date, client: MeteredClient,
    review_enabled: bool = True,
) -> int:
    """The whole daily job in order: ingest, extract, then generate. Stops at the first stage that cannot go on.

    A feed run that stored nothing at all stops the job before any page fetch or model spend (exit 1).
    A failed extract only costs the digest its full text, so the job goes on. The exit code is
    generate's.
    """
    print("== ingest ==")
    if run_ingest(db_path, feeds_path) != 0:
        print("error: no feed could be read; stopping before any model call", file=sys.stderr)
        return 1
    print("\n== extract ==")
    days = (date.today() - target).days + 1  # reaches back to the digest day
    if run_extract(db_path, None, days) != 0:
        print("warning: no article text could be fetched; the digest uses feed text only", file=sys.stderr)
    print("\n== generate ==")
    return run_generate(db_path, out_dir, target, client, review_enabled)


def _refresh_index(out_dir: Path) -> list[str]:
    try:
        return rebuild_index(out_dir)
    except OSError:
        return ["index: could not be rebuilt; it is rebuilt from the saved files on the next save"]


class _PrintableFormatter(logging.Formatter):
    """Log lines can carry feed or model text, so they get the same cleaning as printed output."""

    def format(self, record: logging.LogRecord) -> str:
        return _printable(super().format(record))


@contextmanager
def _debug_logging(enabled: bool) -> Iterator[None]:
    """Show the app's own debug log on stderr. The SDK's logging (ANTHROPIC_LOG) is left alone."""
    if not enabled:
        yield
        return
    logger = logging.getLogger("daily_digest")
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_PrintableFormatter("%(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield
    finally:
        logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


def main() -> None:
    parser = argparse.ArgumentParser(prog="daily-digest")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest", help="read the configured RSS feeds into the local database")
    p.add_argument("--db", type=Path, default=None, help="database file (default: <data dir>/digest.db)")
    p.add_argument("--feeds", type=Path, default=DEFAULT_FEEDS_PATH, help="feed list (TOML)")
    e = sub.add_parser("extract", help="fetch full article text for items whose feed text is too short")
    e.add_argument("--db", type=Path, default=None, help="database file (default: <data dir>/digest.db)")
    e.add_argument("--limit", type=int, default=None, help="maximum number of articles to fetch")
    e.add_argument(
        "--days", type=_positive_int, default=DEFAULT_EXTRACT_DAYS,
        help="only fetch items published in the last N days (default 2: yesterday and today)",
    )
    d = sub.add_parser("draft", help="write and review the digest from one day's stored items (no fetching)")
    d.add_argument("--db", type=Path, default=None, help="database file (default: <data dir>/digest.db)")
    d.add_argument(
        "--date", type=date.fromisoformat, default=None, metavar="YYYY-MM-DD",
        help="digest day, local time (default: yesterday)",
    )
    d.add_argument("--stub", action="store_true", help="offline placeholder summaries; no model, no key")
    d.add_argument("--debug", action="store_true", help="log every model call to stderr")
    d.add_argument(
        "--no-review", action="store_true",
        help="skip the Reviewer (cheaper; the digest is marked NOT REVIEWED and the exit code is 3)",
    )
    g = sub.add_parser("generate", help="make yesterday's digest for real: review it, save it, mark its items seen")
    g.add_argument("--db", type=Path, default=None, help="database file (default: <data dir>/digest.db)")
    g.add_argument("--out-dir", type=Path, default=None, help="where digests go (default: newsletters/ in the project)")
    g.add_argument(
        "--date", type=date.fromisoformat, default=None, metavar="YYYY-MM-DD",
        help="digest day, local time, earlier than today (default: yesterday)",
    )
    g.add_argument("--debug", action="store_true", help="log every model call and review round to stderr")
    g.add_argument(
        "--no-review", action="store_true",
        help="skip the Reviewer; nothing is saved and the exit code is 3 (to test the pipeline cheaply)",
    )
    r = sub.add_parser(
        "run", help="the whole daily job: ingest, extract, then generate yesterday's digest (one command)"
    )
    r.add_argument("--db", type=Path, default=None, help="database file (default: <data dir>/digest.db)")
    r.add_argument("--feeds", type=Path, default=DEFAULT_FEEDS_PATH, help="feed list (TOML)")
    r.add_argument("--out-dir", type=Path, default=None, help="where digests go (default: newsletters/ in the project)")
    r.add_argument(
        "--date", type=date.fromisoformat, default=None, metavar="YYYY-MM-DD",
        help="digest day, local time, earlier than today (default: yesterday)",
    )
    r.add_argument("--debug", action="store_true", help="log every model call and review round to stderr")
    r.add_argument(
        "--no-review", action="store_true",
        help="skip the Reviewer; nothing is saved and the exit code is 3 (to test the pipeline cheaply)",
    )
    args = parser.parse_args()
    if args.command in ("generate", "run"):
        target = args.date or date.today() - timedelta(days=1)
        if target >= date.today():
            print("error: --date must be earlier than today", file=sys.stderr)
            raise SystemExit(2)
        out_dir = args.out_dir or DEFAULT_NEWSLETTERS_DIR
        existing = digest_path(out_dir, target)
        if existing.exists():  # before any fetch, key check or model spend
            print(_printable(f"ALREADY_EXISTS: {existing}"))
            raise SystemExit(0)
        client = _build_client(DEFAULT_SETTINGS_PATH)  # a missing key stops `run` before it fetches anything
        if client is None:
            raise SystemExit(2)
        db_path = args.db or data_dir() / "digest.db"
        with _debug_logging(args.debug):
            if args.command == "run":
                raise SystemExit(run_pipeline(db_path, args.feeds, out_dir, target, client, not args.no_review))
            raise SystemExit(run_generate(db_path, out_dir, target, client, not args.no_review))
    if args.command == "draft":
        target = args.date or date.today() - timedelta(days=1)
        client = None
        if not args.stub:
            client = _build_client(DEFAULT_SETTINGS_PATH)
            if client is None:
                raise SystemExit(2)
        with _debug_logging(args.debug):
            raise SystemExit(run_draft(args.db or data_dir() / "digest.db", target, client, not args.no_review))
    if args.command == "extract":
        raise SystemExit(run_extract(args.db or data_dir() / "digest.db", args.limit, args.days))
    if args.command == "ingest":
        raise SystemExit(run_ingest(args.db or data_dir() / "digest.db", args.feeds))
