"""SQLite storage (CONTRACT.md section 7). One database file, one connection per thread.

Modules create their own tables, always prefixed with ``<module>_``, through :meth:`Store.script`.
Writes are serialised in-process by a lock; reads never take it.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS favourites (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    subtitle TEXT NOT NULL DEFAULT '',
    lat REAL,
    lon REAL,
    layer TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    UNIQUE(kind, ref)
);
CREATE TABLE IF NOT EXISTS casefiles (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS casefile_items (
    id INTEGER PRIMARY KEY,
    casefile_id INTEGER NOT NULL REFERENCES casefiles(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    subtitle TEXT NOT NULL DEFAULT '',
    lat REAL,
    lon REAL,
    layer TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    note TEXT NOT NULL DEFAULT '',
    added_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS casefile_items_cf ON casefile_items(casefile_id);
"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.write_lock = threading.RLock()
        self.script(CORE_SCHEMA)

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=15, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=15000")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    def execute(self, sql: str, params: Iterable[Any] | dict = ()) -> sqlite3.Cursor:
        with self.write_lock:
            return self.conn().execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self.tx() as c:
            c.executemany(sql, rows)

    def query(self, sql: str, params: Iterable[Any] | dict = ()) -> list[dict]:
        return [dict(row) for row in self.conn().execute(sql, params).fetchall()]

    def one(self, sql: str, params: Iterable[Any] | dict = ()) -> dict | None:
        row = self.conn().execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def scalar(self, sql: str, params: Iterable[Any] | dict = (), default: Any = None) -> Any:
        row = self.conn().execute(sql, params).fetchone()
        return row[0] if row is not None and row[0] is not None else default

    def script(self, sql: str) -> None:
        with self.write_lock:
            self.conn().executescript(sql)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.write_lock:
            c = self.conn()
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT v FROM kv WHERE k = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["v"])
        except ValueError:
            return default

    def kv_set(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO kv(k, v, updated_at) VALUES(?, ?, ?) "
                     "ON CONFLICT(k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at",
                     (key, json.dumps(value), time.time()))

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None
