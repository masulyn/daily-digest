"""Command-line entry point (FR-7.12). Only `ingest` exists so far."""

import argparse
from pathlib import Path

from .config import PROJECT_ROOT, load_sources
from .fetch import HttpFetcher, user_agent
from .ingest import ingest
from .robots import RobotsChecker
from .store import Store

DEFAULT_DB = PROJECT_ROOT / "data" / "digest.db"


def run_ingest(db_path: Path) -> int:
    sources = load_sources()
    fetcher = HttpFetcher()
    robots = RobotsChecker(fetcher.fetch, user_agent())
    store = Store(db_path)
    try:
        reports = ingest(sources, fetcher.fetch, robots, store)
        print(f"{'source':34} {'entries':>7} {'stored':>6} {'dupes':>5} {'skipped':>7}  error")
        for r in reports:
            print(f"{r.source:34} {r.entries:7} {r.stored:6} {r.duplicates:5} {r.skipped:7}  {r.error or ''}")
        print(f"\nItems in database: {store.count()}  ({db_path})")
    finally:
        store.close()
    return 0 if all(r.error is None for r in reports) else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="daily-digest")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest", help="read the configured RSS feeds into the local database")
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    if args.command == "ingest":
        raise SystemExit(run_ingest(args.db))
