"""Tiny SQLite wrapper — the strategies' source of truth (spec 3.3).

Path-based db, WAL journal, ISO-8601 UTC timestamps; atomic `transaction`
contextmanager so every state transition is written as one unit (crash-safe:
killing the process never leaves a half-written transition, spec 3.3).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """ISO-8601 UTC timestamp string (the only timestamp format we store)."""
    return utcnow().isoformat(timespec="milliseconds")


class DeltaSchema:
    """Base schema wrapper. Subclasses call `init()` creating their tables."""

    def __init__(self, db_path: str | Path, table_prefix: str = "") -> None:
        self._path = str(Path(db_path))
        self._prefix = table_prefix
        self._conn: sqlite3.Connection | None = None
        self.init()

    # ------------------------------------------------------------- plumbing
    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def init(self) -> None:
        """Create the shared `events` table (spec 3.5) + any subclass schema.
        Subclasses: call super().init() then CREATE TABLE IF NOT EXISTS ..."""
        self.exec(
            f"CREATE TABLE IF NOT EXISTS {self._prefix}events ("
            " ts TEXT NOT NULL, algo TEXT NOT NULL, kind TEXT NOT NULL,"
            " symbol TEXT, message TEXT NOT NULL, features TEXT)"
        )

    # --------------------------------------------------------------- queries
    def exec(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> None:
        cur = self.conn.execute(sql, tuple(params) if isinstance(params, (list, tuple)) else params)
        self.conn.commit()
        cur.close()

    def query(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[dict[str, Any]]:
        cur = self.conn.execute(sql, tuple(params) if isinstance(params, (list, tuple)) else params)
        rows = [dict(r) for r in cur.fetchall()]
        cur.close()
        return rows

    def query_one(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> dict[str, Any] | None:
        cur = self.conn.execute(sql, tuple(params) if isinstance(params, (list, tuple)) else params)
        row = cur.fetchone()
        cur.close()
        return dict(row) if row is not None else None

    # ---------------------------------------------------------- transactions
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic unit: commit on clean exit, roll back on any error (spec 3.3)."""
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            pass  # already inside an explicit transaction; nest into it
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @property
    def table_prefix(self) -> str:
        return self._prefix

    def table(self, name: str) -> str:
        """Prefixed table name (e.g. 'lots' -> 'rwa_lots' when prefix='rwa_')."""
        return self._prefix + name

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
