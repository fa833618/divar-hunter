"""SQLite store of already-seen listing tokens (duplicate-alert prevention).

The store answers one question every cycle: *which of these posts are new?*
Keeping that state on disk means a restart, a crash or a redeploy does not
re-send the last 24 listings to Telegram.

Two tables matter:

``seen_tokens``   one row per listing we have ever looked at
``run_log``       one row per monitoring cycle, for post-mortems

The class is safe to use from several threads (``main.py`` runs blocking work
in a thread pool), and accepts ``:memory:`` paths so tests need no filesystem.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from divar_client import DivarPost

__all__ = ["SeenStore", "SeenRecord", "RunStats"]

LOGGER = logging.getLogger("divar.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_tokens (
    token         TEXT PRIMARY KEY,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    title         TEXT,
    price         INTEGER,
    city          TEXT,
    district      TEXT,
    notified      INTEGER NOT NULL DEFAULT 0,
    notified_at   TEXT,
    is_deal       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_seen_last_seen ON seen_tokens(last_seen_at);

CREATE TABLE IF NOT EXISTS run_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    fetched     INTEGER NOT NULL DEFAULT 0,
    fresh       INTEGER NOT NULL DEFAULT 0,
    deals       INTEGER NOT NULL DEFAULT 0,
    sent        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass(frozen=True)
class SeenRecord:
    """A row from ``seen_tokens``."""

    token: str
    first_seen_at: str
    last_seen_at: str
    title: Optional[str] = None
    price: Optional[int] = None
    city: Optional[str] = None
    district: Optional[str] = None
    notified: bool = False
    notified_at: Optional[str] = None
    is_deal: bool = False


@dataclass
class RunStats:
    """Counters for a single monitoring cycle, persisted into ``run_log``."""

    fetched: int = 0
    fresh: int = 0
    deals: int = 0
    sent: int = 0
    failed: int = 0
    note: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def as_row(self) -> Tuple[str, Optional[str], int, int, int, int, int, str]:
        return (
            _iso(self.started_at or datetime.now(timezone.utc)),
            _iso(self.finished_at),
            self.fetched,
            self.fresh,
            self.deals,
            self.sent,
            self.failed,
            self.note[:500],
        )


def _iso(moment: Optional[datetime]) -> str:
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


class SeenStore:
    """Thread-safe SQLite wrapper for seen tokens."""

    def __init__(self, path: str = "data/seen_tokens.db", prune_days: int = 14) -> None:
        self.path = path
        self.prune_days = prune_days
        self._lock = threading.RLock()
        if path != ":memory:":
            parent = Path(path).expanduser().parent
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ #
    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cursor = self._conn.cursor()
            try:
                yield cursor
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cursor.close()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass

    def __enter__(self) -> "SeenStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def unseen(self, posts: Sequence[DivarPost]) -> List[DivarPost]:
        """Return the posts whose token we have never recorded."""
        if not posts:
            return []
        tokens = [post.token for post in posts if post.token]
        known = set(self.known_tokens(tokens))
        return [post for post in posts if post.token and post.token not in known]

    def known_tokens(self, tokens: Iterable[str]) -> List[str]:
        """Which of ``tokens`` are already in the store? (chunked IN query)."""
        wanted = [t for t in dict.fromkeys(tokens) if t]
        if not wanted:
            return []
        found: List[str] = []
        chunk_size = 400
        with self._cursor() as cursor:
            for start in range(0, len(wanted), chunk_size):
                chunk = wanted[start : start + chunk_size]
                placeholders = ",".join("?" * len(chunk))
                cursor.execute(
                    f"SELECT token FROM seen_tokens WHERE token IN ({placeholders})", chunk
                )
                found.extend(row["token"] for row in cursor.fetchall())
        return found

    def contains(self, token: str) -> bool:
        return bool(self.known_tokens([token]))

    def mark_seen(
        self,
        posts: Sequence[DivarPost],
        *,
        notified_tokens: Iterable[str] = (),
        deal_tokens: Iterable[str] = (),
        now: Optional[datetime] = None,
    ) -> int:
        """Insert/refresh rows for ``posts``. Returns how many rows were written."""
        if not posts:
            return 0
        moment = _iso(now or datetime.now(timezone.utc))
        notified = set(notified_tokens)
        deals = set(deal_tokens)
        rows = [
            (
                post.token,
                moment,
                moment,
                (post.title or "")[:300],
                post.price,
                post.city,
                post.district,
                1 if post.token in notified else 0,
                moment if post.token in notified else None,
                1 if post.token in deals else 0,
            )
            for post in posts
            if post.token
        ]
        if not rows:
            return 0
        with self._cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO seen_tokens (
                    token, first_seen_at, last_seen_at, title, price, city, district,
                    notified, notified_at, is_deal
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(token) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    title        = COALESCE(excluded.title, seen_tokens.title),
                    price        = COALESCE(excluded.price, seen_tokens.price),
                    notified     = MAX(seen_tokens.notified, excluded.notified),
                    notified_at  = COALESCE(excluded.notified_at, seen_tokens.notified_at),
                    is_deal      = MAX(seen_tokens.is_deal, excluded.is_deal)
                """,
                rows,
            )
        return len(rows)

    def mark_notified(self, tokens: Iterable[str], now: Optional[datetime] = None) -> int:
        """Flag tokens as alerted (used after a successful Telegram send)."""
        wanted = [t for t in tokens if t]
        if not wanted:
            return 0
        moment = _iso(now or datetime.now(timezone.utc))
        with self._cursor() as cursor:
            cursor.executemany(
                "UPDATE seen_tokens SET notified = 1, notified_at = ? WHERE token = ?",
                [(moment, token) for token in wanted],
            )
            return cursor.rowcount if cursor.rowcount > 0 else len(wanted)

    # ------------------------------------------------------------------ #
    def get(self, token: str) -> Optional[SeenRecord]:
        with self._cursor() as cursor:
            cursor.execute("SELECT * FROM seen_tokens WHERE token = ?", (token,))
            row = cursor.fetchone()
        if row is None:
            return None
        return SeenRecord(
            token=row["token"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            title=row["title"],
            price=row["price"],
            city=row["city"],
            district=row["district"],
            notified=bool(row["notified"]),
            notified_at=row["notified_at"],
            is_deal=bool(row["is_deal"]),
        )

    def count(self) -> int:
        with self._cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS n FROM seen_tokens")
            return int(cursor.fetchone()["n"])

    def deal_count(self) -> int:
        with self._cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS n FROM seen_tokens WHERE is_deal = 1")
            return int(cursor.fetchone()["n"])

    def prune(self, now: Optional[datetime] = None) -> int:
        """Delete tokens older than ``prune_days``. Returns rows deleted."""
        if self.prune_days <= 0:
            return 0
        moment = (now or datetime.now(timezone.utc)) - timedelta(days=self.prune_days)
        with self._cursor() as cursor:
            cursor.execute("DELETE FROM seen_tokens WHERE last_seen_at < ?", (_iso(moment),))
            deleted = cursor.rowcount
        if deleted:
            LOGGER.info("pruned %d seen token(s) older than %d days", deleted, self.prune_days)
        return max(0, deleted)

    # ------------------------------------------------------------------ #
    def meta_get(self, key: str) -> Optional[str]:
        with self._cursor() as cursor:
            cursor.execute("SELECT value FROM meta WHERE key = ?", (key,))
            row = cursor.fetchone()
        return row["value"] if row else None

    def meta_set(self, key: str, value: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    @property
    def is_bootstrapped(self) -> bool:
        """True once a first run has seeded the store (see BOOTSTRAP_SILENT)."""
        return self.meta_get("bootstrapped_at") not in (None, "")

    def mark_bootstrapped(self, now: Optional[datetime] = None) -> None:
        self.meta_set("bootstrapped_at", _iso(now or datetime.now(timezone.utc)))

    # ------------------------------------------------------------------ #
    def log_run(self, stats: RunStats) -> int:
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO run_log (
                    started_at, finished_at, fetched, fresh, deals, sent, failed, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                stats.as_row(),
            )
            return int(cursor.lastrowid or 0)

    def recent_runs(self, limit: int = 10) -> List[sqlite3.Row]:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT * FROM run_log ORDER BY id DESC LIMIT ?", (max(1, limit),)
            )
            return list(cursor.fetchall())

    def stats(self) -> Dict[str, object]:
        """Snapshot of the store, for ``main.py --stats`` and startup logging."""
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(notified) AS notified,
                       SUM(is_deal) AS deals,
                       MIN(first_seen_at) AS oldest,
                       MAX(last_seen_at) AS newest
                FROM seen_tokens
                """
            )
            row = cursor.fetchone()
            cursor.execute("SELECT COUNT(*) AS runs FROM run_log")
            runs = int(cursor.fetchone()["runs"])
        return {
            "db_path": self.path,
            "tokens": int(row["total"] or 0),
            "notified": int(row["notified"] or 0),
            "deals": int(row["deals"] or 0),
            "runs": runs,
            "oldest": row["oldest"],
            "newest": row["newest"],
            "bootstrapped_at": self.meta_get("bootstrapped_at"),
        }
