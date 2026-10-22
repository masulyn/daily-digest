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
    """
    BEGIN;
    ALTER TABLE items ADD COLUMN article_text TEXT;
    ALTER TABLE items ADD COLUMN needs_extraction INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE items ADD COLUMN extraction_attempted_at TEXT;
    ALTER TABLE items ADD COLUMN extraction_error TEXT;
    -- Rows stored before this version are judged by the length of their stored extract only;
    -- unlike new rows, a long extract that ends like a teaser is not flagged.
    UPDATE items SET needs_extraction = CASE WHEN length(extract) < 300 THEN 1 ELSE 0 END;
    CREATE INDEX idx_items_pending ON items(needs_extraction, extraction_attempted_at);
    PRAGMA user_version = 2;
    COMMIT;
    """,
    """
    BEGIN;
    ALTER TABLE items ADD COLUMN extraction_attempts INTEGER NOT NULL DEFAULT 0;
    UPDATE items SET extraction_attempts = 1 WHERE extraction_attempted_at IS NOT NULL;
    PRAGMA user_version = 3;
    COMMIT;
    """,
    """
    BEGIN;
    -- The feed body is gone, so the ellipsis make_extract appends at near-full length is the
    -- only trace of a cut.
    UPDATE items SET needs_extraction = 1
    WHERE article_text IS NULL AND extraction_attempts = 0 AND extraction_attempted_at IS NULL
      AND extract LIKE '%…' AND length(extract) >= 1000;
    PRAGMA user_version = 4;
    COMMIT;
    """,
]
MAX_EXTRACTION_ATTEMPTS = 3


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


class DatabaseNotFound(Exception):
    pass


class Store:
    def __init__(self, path: Path | str, *, must_exist: bool = False) -> None:
        # must_exist needs a real file, so ":memory:" is not allowed with it.
        # sqlite3.connect would silently create a mistyped path as an empty database.
        if must_exist and not Path(path).is_file():
            raise DatabaseNotFound(f"database not found: {path}; run 'daily-digest ingest' first")
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

    def add_if_new(self, item: CandidateItem, needs_extraction: bool = False) -> bool:
        """Store the item unless its URL or content hash is already stored.

        Returns True if stored, False if it was a duplicate.
        """
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO items (id, source, canonical_url, content_hash, title,"
            " published_at, extract, first_seen_at, needs_extraction) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.id,
                item.source,
                item.canonical_url,
                item.content_hash,
                item.title,
                _utc(item.published),
                item.extract,
                _utc(datetime.now(timezone.utc)),
                int(needs_extraction),
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
            "SELECT id, source, canonical_url, title, published_at, extract, content_hash, article_text"
            f" FROM items WHERE published_at >= ? AND published_at < ?{unseen}"
            " ORDER BY published_at DESC",
            (_utc(start), _utc(end)),
        ).fetchall()
        return [
            CandidateItem(
                id=r[0], source=r[1], canonical_url=r[2], title=r[3],
                published=datetime.fromisoformat(r[4]), extract=r[5], content_hash=r[6],
                article_text=r[7],
            )
            for r in rows
        ]

    def pending_extraction(
        self, limit: int | None = None, since: datetime | None = None
    ) -> list[tuple[str, str]]:
        """(id, URL) of items flagged for extraction that have not been attempted, newest first.

        With `since`, only items published at or after it; the rest stay pending untouched.
        """
        sql = (
            "SELECT id, canonical_url FROM items WHERE needs_extraction = 1"
            " AND article_text IS NULL AND extraction_attempted_at IS NULL"
            " AND extraction_attempts < ?"
        )
        params: tuple = (MAX_EXTRACTION_ATTEMPTS,)
        if since is not None:
            sql += " AND published_at >= ?"
            params += (_utc(since),)
        sql += " ORDER BY published_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params += (limit,)
        return [(r[0], r[1]) for r in self._db.execute(sql, params).fetchall()]

    def source_of(self, item_id: str) -> str | None:
        """The name of the feed an item came from."""
        row = self._db.execute("SELECT source FROM items WHERE id = ?", (item_id,)).fetchone()
        return row[0] if row else None

    def save_article_text(self, item_id: str, text: str) -> None:
        self._db.execute(
            "UPDATE items SET article_text = ?, extraction_attempted_at = ?, extraction_error = NULL"
            " WHERE id = ?",
            (text, _utc(datetime.now(timezone.utc)), item_id),
        )

    def record_extraction_failure(self, item_id: str, code: str, *, final: bool) -> None:
        """Record a failed attempt.

        A final failure (the page is blocked, gone, or has no article) closes the item. A
        temporary one leaves it pending for the next run, until MAX_EXTRACTION_ATTEMPTS.
        """
        self._db.execute(
            "UPDATE items SET extraction_attempts = extraction_attempts + 1, extraction_error = ?,"
            " extraction_attempted_at = CASE WHEN ? OR extraction_attempts + 1 >= ? THEN ? END"
            " WHERE id = ?",
            (code, int(final), MAX_EXTRACTION_ATTEMPTS, _utc(datetime.now(timezone.utc)), item_id),
        )
