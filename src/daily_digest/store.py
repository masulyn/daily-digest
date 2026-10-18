"""SQLite storage. Only this module touches the database file.

Two separate concepts:
- `items`: everything fetched, de-duplicated by canonical URL and by content hash.
- `seen_items`: the ledger of items already reported in a published digest. It is written
  only after publication, so a failed run does not use up items.
"""

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from .contracts import CandidateItem

# Each entry upgrades the schema by one version; never edit one that has shipped.
_MIGRATIONS = [
    """
    BEGIN;
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
    DROP INDEX IF EXISTS idx_items_hash;
    CREATE UNIQUE INDEX IF NOT EXISTS idx_items_hash_unique ON items(content_hash);
    CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_at);
    CREATE TABLE seen_items (
        canonical_url TEXT PRIMARY KEY,
        content_hash TEXT NOT NULL,
        digest_date TEXT NOT NULL,
        first_seen_at TEXT NOT NULL
    );
    CREATE INDEX idx_seen_hash ON seen_items(content_hash);
    PRAGMA user_version = 1;
    COMMIT;
    """,
]


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Autocommit; multi-statement atomicity is explicit via transaction().
        self._db = sqlite3.connect(str(path), isolation_level=None)
        self._migrate()

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        for script in _MIGRATIONS[version:]:
            self._db.executescript(script)

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit everything inside the block together, or none of it if it raises.

        Inside an outer transaction this joins it; the outermost block decides.
        """
        if self._db.in_transaction:
            yield
            return
        self._db.execute("BEGIN")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def add_if_new(self, item: CandidateItem) -> bool:
        """Store the item unless its URL or content hash is already stored.

        Returns True if stored, False if it was a duplicate.
        """
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO items (id, source, canonical_url, content_hash, title,"
            " published_at, extract, first_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.id,
                item.source,
                item.canonical_url,
                item.content_hash,
                item.title,
                _utc(item.published),
                item.extract,
                _utc(datetime.now(timezone.utc)),
            ),
        )
        return cursor.rowcount == 1

    def count(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def mark_seen(self, items: Iterable[CandidateItem], digest_date: date) -> None:
        """Record items as reported in the digest for `digest_date`."""
        now = _utc(datetime.now(timezone.utc))
        with self.transaction():
            self._db.executemany(
                "INSERT OR IGNORE INTO seen_items (canonical_url, content_hash, digest_date,"
                " first_seen_at) VALUES (?, ?, ?, ?)",
                [(i.canonical_url, i.content_hash, digest_date.isoformat(), now) for i in items],
            )

    def items_published_between(
        self, start: datetime, end: datetime, *, include_seen: bool = False
    ) -> list[CandidateItem]:
        """Items published in [start, end), newest first, excluding already-reported ones by default."""
        unseen = "" if include_seen else (
            " AND NOT EXISTS (SELECT 1 FROM seen_items s"
            " WHERE s.canonical_url = items.canonical_url OR s.content_hash = items.content_hash)"
        )
        rows = self._db.execute(
            "SELECT id, source, canonical_url, title, published_at, extract, content_hash"
            f" FROM items WHERE published_at >= ? AND published_at < ?{unseen}"
            " ORDER BY published_at DESC",
            (_utc(start), _utc(end)),
        ).fetchall()
        return [
            CandidateItem(
                id=r[0], source=r[1], canonical_url=r[2], title=r[3],
                published=datetime.fromisoformat(r[4]), extract=r[5], content_hash=r[6],
            )
            for r in rows
        ]
