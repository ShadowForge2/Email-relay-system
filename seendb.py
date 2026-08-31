"""Persistent seen-address store (SQLite).

Tracks two facts across restarts:
  * which email addresses were already POLLED (written to output files)
  * which email addresses were already MAILED (a send was attempted)

Poller and sender both consume the same DB file, so an address is never
re-polled and never re-mailed, even across separate runs/processes/days.

Usage:
    store = SeenStore("seen.sqlite")
    if store.register_polled("a@example.com"):
        ...   # brand new, safe to output
    if store.register_mailed("a@example.com"):
        ...   # brand new, safe to send
"""

import sqlite3
import threading


class SeenStore:
    """Thread-safe SQLite-backed dedupe set, scoped to two tables."""

    def __init__(self, path: str):
        self._path = path
        self._conn = sqlite3.connect(path, timeout=30)
        self._conn.execute("CREATE TABLE IF NOT EXISTS polled (email TEXT PRIMARY KEY)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS mailed (email TEXT PRIMARY KEY)")
        self._conn.commit()
        self._lock = threading.Lock()

    def _insert(self, table: str, email: str) -> bool:
        email = str(email).strip().lower()
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO {} (email) VALUES (?)".format(table), (email,)
            )
            self._conn.commit()
            return cur.rowcount == 1

    def register_polled(self, email: str) -> bool:
        """True if this address has never been polled before."""
        return self._insert("polled", email)

    def register_mailed(self, email: str) -> bool:
        """True if this address has never been mailed before."""
        return self._insert("mailed", email)

    def seen_mailed(self, email: str) -> bool:
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM mailed WHERE email = ?", (str(email).strip().lower(),))
            return cur.fetchone() is not None

    def polled_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM polled").fetchone()
            return int(row[0])

    def clear_polled(self) -> int:
        """Delete every recorded polled address. Returns rows removed."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM polled")
            self._conn.commit()
            return cur.rowcount

    def mailed_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM mailed").fetchone()
            return int(row[0])

    def close(self):
        with self._lock:
            self._conn.close()