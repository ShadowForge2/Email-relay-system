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

import os
import sqlite3
import threading


class PgSeenStore:
    """PostgreSQL-backed dedupe set (persists across restarts/deploys).

    Swaps in for SeenStore whenever a DATABASE_URL is provided (used on
    Render, where the local filesystem is ephemeral). Implements the same
    public interface: register_polled / register_mailed / seen_mailed /
    polled_count / mailed_count / close.
    """

    def __init__(self, dsn: str = None):
        try:
            import psycopg
        except Exception as e:  # pragma: no cover - dep missing
            raise RuntimeError("psycopg not installed (pip install psycopg[binary])") from e
        self._dsn = dsn or os.environ.get("DATABASE_URL")
        if not self._dsn:
            raise ValueError("PgSeenStore requires a DATABASE_URL")
        self._conn = psycopg.connect(self._dsn)
        self._conn.autocommit = True
        with self._conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS polled (email VARCHAR PRIMARY KEY)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS mailed (email VARCHAR PRIMARY KEY)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sent_daily ("
                "  day VARCHAR NOT NULL, label VARCHAR NOT NULL, n INTEGER NOT NULL DEFAULT 0,"
                "  PRIMARY KEY (day, label)"
                ")"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS control (k VARCHAR PRIMARY KEY, v VARCHAR)"
            )
        self._lock = threading.Lock()

    def _insert(self, table: str, email: str) -> bool:
        email = str(email).strip().lower()
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO {} (email) VALUES (%s) ON CONFLICT DO NOTHING".format(table),
                    (email,),
                )
                return cur.rowcount == 1

    def register_polled(self, email: str) -> bool:
        return self._insert("polled", email)

    def register_mailed(self, email: str) -> bool:
        return self._insert("mailed", email)

    def seen_mailed(self, email: str) -> bool:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM mailed WHERE email = %s",
                    (str(email).strip().lower(),),
                )
                return cur.fetchone() is not None

    def polled_count(self) -> int:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM polled")
                return int(cur.fetchone()[0])

    def mailed_count(self) -> int:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM mailed")
                return int(cur.fetchone()[0])

    def clear_polled(self) -> int:
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute("DELETE FROM polled")
                return cur.rowcount

    def close(self):
        with self._lock:
            self._conn.close()


def store_from_env(sqlite_path: str = "seen.sqlite"):
    """Return the right seen-store for the current environment.

    If DATABASE_URL is set (Render), return a Postgres store so dedupe
    survives restarts; otherwise fall back to the local SQLite file.
    """
    if os.environ.get("DATABASE_URL"):
        try:
            return PgSeenStore()
        except Exception:
            pass
    return SeenStore(sqlite_path)


class PgDailyCount:
    """Persist today's sent-count per provider so restarts don't leak past daily caps.

    The in-memory DailySlots resets whenever the process restarts (common on
    Render free). This table records how many messages each provider has
    actually sent today, keyed by (day, label). The scheduler consults it as a
    hard guard on top of the provider's daily_cap.
    """

    def __init__(self, store):
        self._conn = store._conn
        self._lock = store._lock

    @staticmethod
    def _today():
        from datetime import date
        return date.today().isoformat()

    def used_today(self, label: str) -> int:
        today = self._today()
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT n FROM sent_daily WHERE day = %s AND label = %s",
                    (today, label),
                )
                row = cur.fetchone()
                return int(row[0]) if row else 0

    def claim(self, label: str, cap: int) -> bool:
        today = self._today()
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT n FROM sent_daily WHERE day = %s AND label = %s",
                    (today, label),
                )
                row = cur.fetchone()
                n = int(row[0]) if row else 0
                if n >= cap:
                    return False
                if row:
                    cur.execute(
                        "UPDATE sent_daily SET n = n + 1 "
                        "WHERE day = %s AND label = %s",
                        (today, label),
                    )
                else:
                    cur.execute(
                        "INSERT INTO sent_daily (day, label, n) VALUES (%s, %s, 1)",
                        (today, label),
                    )
                return True

    def increment(self, label: str) -> int:
        """Record one sent message for ``label`` today; return new count."""
        today = self._today()
        with self._lock:
            with self._conn.cursor() as cur:
                cur.execute(
                    "SELECT n FROM sent_daily WHERE day = %s AND label = %s",
                    (today, label),
                )
                row = cur.fetchone()
                n = int(row[0]) if row else 0
                n += 1
                if row:
                    cur.execute(
                        "UPDATE sent_daily SET n = %s WHERE day = %s AND label = %s",
                        (n, today, label),
                    )
                else:
                    cur.execute(
                        "INSERT INTO sent_daily (day, label, n) VALUES (%s, %s, %s)",
                        (today, label, n),
                    )
                return n


class SeenStore:
    """Thread-safe SQLite-backed dedupe set, scoped to two tables."""

    def __init__(self, path: str):
        self._path = path
        self._conn = sqlite3.connect(path, timeout=30)
        self._conn.execute("CREATE TABLE IF NOT EXISTS polled (email TEXT PRIMARY KEY)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS mailed (email TEXT PRIMARY KEY)")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS control (k TEXT PRIMARY KEY, v TEXT)"
        )
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


class CampaignControl:
    """Persistent start/stop switch backed by a seen-store connection.

    Works with both SQLite (SeenStore) and Postgres (PgSeenStore) by reading
    the store's ``_conn``. `key=true` starts sending, `key=false` stops.
    """

    def __init__(self, store):
        self._conn = store._conn
        self._lock = store._lock
        self._pg = "Pg" in type(store).__name__

    def set(self, key: str, value: str) -> str:
        with self._lock:
            if self._pg:
                with self._conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO control (k, v) VALUES (%s, %s) "
                        "ON CONFLICT (k) DO UPDATE SET v = %s",
                        (key, str(value), str(value)),
                    )
            else:
                self._conn.execute(
                    "INSERT OR REPLACE INTO control (k, v) VALUES (?, ?)",
                    (key, str(value)),
                )
                self._conn.commit()
        return str(value)

    def get(self, key: str, default: str = "") -> str:
        with self._lock:
            if self._pg:
                with self._conn.cursor() as cur:
                    cur.execute("SELECT v FROM control WHERE k = %s", (key,))
                    row = cur.fetchone()
            else:
                cur = self._conn.execute("SELECT v FROM control WHERE k = ?", (key,))
                row = cur.fetchone()
            return str(row[0]) if row else default

    def active(self) -> bool:
        return self.get("campaign_active", "").strip().lower() == "true"