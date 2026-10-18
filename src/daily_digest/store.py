"""SQLite storage for fetched items and the seen-items ledger (FR-2.x).

Only this module touches the database file. Duplicates are caught by canonical URL
or by content hash (the same story at a different address).
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .contracts import CandidateItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT NOT NULL,
    extract TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_hash ON items(content_hash);
CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_at);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.executescript(SCHEMA)

    def add_if_new(self, item: CandidateItem) -> bool:
        """Insert the item unless its URL or content hash is already stored.

        Returns True if stored, False if it was a duplicate.
        """
        seen = self._db.execute(
            "SELECT 1 FROM items WHERE canonical_url = ? OR content_hash = ?",
            (item.canonical_url, item.content_hash),
        ).fetchone()
        if seen:
            return False
        self._db.execute(
            "INSERT INTO items (id, source, canonical_url, content_hash, title, published_at,"
            " extract, first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.id,
                item.source,
                item.canonical_url,
                item.content_hash,
                item.title,
                item.published.astimezone(timezone.utc).isoformat(),
                item.extract,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self._db.commit()
        return True

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def items_published_between(self, start: datetime, end: datetime) -> list[CandidateItem]:
        """Items published in [start, end), newest first."""
        rows = self._db.execute(
            "SELECT id, source, canonical_url, title, published_at, extract, content_hash"
            " FROM items WHERE published_at >= ? AND published_at < ? ORDER BY published_at DESC",
            (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
        ).fetchall()
        return [
            CandidateItem(
                id=r[0], source=r[1], canonical_url=r[2], title=r[3],
                published=datetime.fromisoformat(r[4]), extract=r[5], content_hash=r[6],
            )
            for r in rows
        ]

    def close(self) -> None:
        self._db.close()
