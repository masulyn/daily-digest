"""Command-line entry point."""

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from .config import DEFAULT_FEEDS_PATH, data_dir, load_sources
from .extract import extract_articles
from .fetch import HttpFetcher, user_agent
from .graph import run_digest
from .ingest import ingest
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
        print(error, file=sys.stderr)
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


def run_extract(db_path: Path, limit: int | None) -> int:
    _warn_if_no_contact()
    store = _open_existing(db_path)
    if store is None:
        return 2
    with store, HttpFetcher() as fetcher:
        robots = RobotsChecker(fetcher.fetch, user_agent())
        report = extract_articles(
            store, fetcher.fetch, robots, set_host_delay=fetcher.set_host_delay, limit=limit
        )
    print(f"Considered {report.considered}, extracted {report.extracted}, deferred {report.deferred}.")
    for code, n in sorted(report.failures.items()):
        print(f"  {code}: {n}")
    # Like ingest: fail only when articles were attempted and none came back.
    return 1 if report.considered and not report.extracted else 0


def run_draft(db_path: Path, target: date) -> int:
    """Print stub summaries for the day's unseen items from the local database. Fetches nothing."""
    store = _open_existing(db_path)
    if store is None:
        return 2
    with store:
        state = run_digest(store, target)
    by_id = {item.id: item for item in state.items}
    for summary in state.summaries:
        item = by_id[summary.item_id]
        print(f"{item.title} ({item.source})")
        print("\n".join(f"- {point}" for point in summary.key_points))
        print(f"  {summary.source_url}")
    for note in state.notes:
        print(note)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="daily-digest")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest", help="read the configured RSS feeds into the local database")
    p.add_argument("--db", type=Path, default=None, help="database file (default: <data dir>/digest.db)")
    p.add_argument("--feeds", type=Path, default=DEFAULT_FEEDS_PATH, help="feed list (TOML)")
    e = sub.add_parser("extract", help="fetch full article text for items whose feed text is too short")
    e.add_argument("--db", type=Path, default=None, help="database file (default: <data dir>/digest.db)")
    e.add_argument("--limit", type=int, default=None, help="maximum number of articles to fetch")
    d = sub.add_parser("draft", help="print stub summaries for one day's stored items (no fetching)")
    d.add_argument("--db", type=Path, default=None, help="database file (default: <data dir>/digest.db)")
    d.add_argument(
        "--date", type=date.fromisoformat, default=None, metavar="YYYY-MM-DD",
        help="digest day, local time (default: yesterday)",
    )
    args = parser.parse_args()
    if args.command == "draft":
        target = args.date or date.today() - timedelta(days=1)
        raise SystemExit(run_draft(args.db or data_dir() / "digest.db", target))
    if args.command == "extract":
        raise SystemExit(run_extract(args.db or data_dir() / "digest.db", args.limit))
    if args.command == "ingest":
        raise SystemExit(run_ingest(args.db or data_dir() / "digest.db", args.feeds))
