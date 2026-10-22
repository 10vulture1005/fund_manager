"""DeltaSchema SQLite wrapper tests on tmp_path: WAL mode, transaction
atomicity, and the shared `events` table write/read round-trip (spec 3.3/3.5)."""
from __future__ import annotations

import os

import pytest

from core.events import EventLogger
from core.state import DeltaSchema, now_iso


@pytest.fixture()
def db(tmp_path):
    d = DeltaSchema(tmp_path / "state.db")
    yield d
    d.close()


class TestWal:
    def test_journal_mode_is_wal(self, db: DeltaSchema):
        mode = db.query_one("PRAGMA journal_mode")["journal_mode"]
        assert str(mode).lower() == "wal"

    def test_wal_files_created(self, tmp_path, db: DeltaSchema):
        db.exec("INSERT INTO events VALUES ('t1','a','info',NULL,'hello','{}')")
        assert (tmp_path / "state.db").exists()


class TestQueries:
    def test_exec_query_round_trip(self, db: DeltaSchema):
        db.exec(
            "CREATE TABLE t (a INTEGER, b TEXT)"
        )
        db.exec("INSERT INTO t VALUES (1, 'x')")
        db.exec("INSERT INTO t VALUES (2, 'y')")
        rows = db.query("SELECT * FROM t ORDER BY a")
        assert rows == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
        assert db.query_one("SELECT a FROM t WHERE b='x'") == {"a": 1}
        assert db.query_one("SELECT a FROM t WHERE b='zzz'") is None

    def test_named_params(self, db: DeltaSchema):
        db.exec("CREATE TABLE t (a INTEGER)")
        db.exec("INSERT INTO t VALUES (:v)", {"v": 42})
        assert db.query_one("SELECT a FROM t") == {"a": 42}


class TestTransactionAtomicity:
    def test_rollback_on_error(self, db: DeltaSchema):
        db.exec("CREATE TABLE t (a INTEGER)")
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.conn.execute("INSERT INTO t VALUES (1)")
                db.conn.execute("INSERT INTO t VALUES (2)")
                raise RuntimeError("crash mid-transaction (simulate kill, spec 3.3)")
        assert db.query("SELECT * FROM t") == []

    def test_commit_on_success(self, db: DeltaSchema):
        db.exec("CREATE TABLE t (a INTEGER)")
        with db.transaction():
            db.conn.execute("INSERT INTO t VALUES (1)")
        assert db.query_one("SELECT a FROM t") == {"a": 1}

    def test_broken_insert_preflight_simulated_crash(self, db: DeltaSchema):
        """Simulate the crash window between 'write state' and 'confirm'
        (spec 3.3): one atomic transaction either lands whole or not at all."""
        db.exec("CREATE TABLE t (a INTEGER, b TEXT)")
        try:
            with db.transaction():
                db.conn.execute("INSERT INTO t VALUES (1, 'half_written')")
                db.conn.execute("INSERT INTO t VALUES (2, 'not_an_int_yet')")
                raise KeyboardInterrupt  # process kill
        except KeyboardInterrupt:
            pass
        assert db.query("SELECT * FROM t") == []


class TestEventsRoundTrip:
    def test_eventlog_write_read(self, tmp_path):
        db = DeltaSchema(tmp_path / "events.db", table_prefix="rwa_")
        logger = EventLogger(db, algo="rwa")
        row_id = logger.log(
            kind="signal",
            message="sweep confirmed on XAUTUSD",
            symbol="XAUTUSD",
            features={"score": 3, "sweep": "sell_side", "atr_1h": 12.5},
            ts=__import__("datetime").datetime(2026, 3, 1, 12, 0, 0),
        )
        assert row_id > 0
        rows = logger.recent(limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row["algo"] == "rwa"
        assert row["kind"] == "signal"
        assert row["symbol"] == "XAUTUSD"
        assert row["features"]["score"] == 3
        assert row["ts"].startswith("2026-03-01T12:00:00")

        # prefix isolation: the default-prefixed table is untouched
        bare = DeltaSchema(tmp_path / "events.db")
        bare_rows = bare.query("SELECT * FROM events")
        assert bare_rows == []

    def test_log_note_from_model(self, tmp_path):
        from core.models import Event, EventNote

        db = DeltaSchema(tmp_path / "n.db")
        logger = EventLogger(db, algo="all_in_one")
        note = EventNote(
            algo="all_in_one", kind=Event.SKIP, symbol="BTCUSD",
            message="spread too wide", features={"spread_pct": 0.25},
        )
        logger.log_note(note)
        row = logger.recent()[0]
        assert row["kind"] == "skip" and row["symbol"] == "BTCUSD"
        assert row["features"]["spread_pct"] == 0.25


def test_now_iso_format():
    stamp = now_iso()
    assert stamp.endswith("+00:00") or stamp.endswith("Z")
    assert stamp[10] == "T"
