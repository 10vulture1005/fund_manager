"""Per-symbol state machine (spec 2.12).

IDLE -> ARMED (zone + bias valid) -> TRIGGERED (sweep confirmed)
     -> BUILDING (rung1 filled, rungs resting)
     -> MANAGING (all/partial filled, bracket live)
     -> EXITING -> COOLDOWN -> IDLE, plus HALTED on any exchange/DB mismatch.

Each transition is PERSISTED ({prefix}symbol_state table + one events row)
BEFORE the corresponding orders are sent (write-before-send, spec 3.3).
"""
from __future__ import annotations

import json
from typing import Any, Optional


TRANSITIONS: dict[str, set[str]] = {
    "IDLE": {"ARMED", "HALTED"},
    "ARMED": {"TRIGGERED", "IDLE", "HALTED"},
    "TRIGGERED": {"BUILDING", "IDLE", "COOLDOWN", "HALTED"},
    "BUILDING": {"MANAGING", "EXITING", "HALTED"},
    "MANAGING": {"EXITING", "HALTED"},
    "EXITING": {"COOLDOWN", "IDLE", "HALTED"},
    "COOLDOWN": {"IDLE", "HALTED"},
    "HALTED": {"IDLE"},   # manual --resume
}

STATES = ("IDLE", "ARMED", "TRIGGERED", "BUILDING", "MANAGING", "EXITING",
          "COOLDOWN", "HALTED")

_SCHEMA = ("CREATE TABLE IF NOT EXISTS {p}symbol_state ("
           " symbol TEXT PRIMARY KEY, state TEXT NOT NULL, features TEXT,"
           " history TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL)")


class IllegalTransition(Exception):
    pass


class StateMachine:
    def __init__(self, db: Any | None = None, events: Any | None = None,
                 algo: str = "rwa_positional", prefix: str = "") -> None:
        """`db` is a core.state.DeltaSchema (creates the symbol_state table);
        `events` is a core.events.EventLogger. Both optional for pure-unit use."""
        self.db = db
        self.events = events
        self.algo = algo
        self.prefix = prefix or ""
        self._memory: dict[str, str] = {}
        if self.db is not None:
            self.db.exec(_SCHEMA.format(p=self.prefix))

    # ------------------------------------------------------------- accessors
    def state_of(self, symbol: str) -> str:
        if symbol in self._memory:
            return self._memory[symbol]
        if self.db is not None:
            row = self.db.query_one(
                f"SELECT state FROM {self.prefix}symbol_state WHERE symbol=?",
                (symbol,))
            if row:
                self._memory[symbol] = row["state"]
                return row["state"]
        return "IDLE"

    def is_state(self, symbol: str, *names: str) -> bool:
        return self.state_of(symbol) in names

    def can(self, symbol: str, new: str) -> bool:
        return new in TRANSITIONS.get(self.state_of(symbol), set())

    def history(self, symbol: str) -> list[str]:
        if self.db is None:
            return []
        row = self.db.query_one(
            f"SELECT history FROM {self.prefix}symbol_state WHERE symbol=?",
            (symbol,))
        if not row:
            return []
        try:
            h = json.loads(row["history"] or "[]")
            return h if isinstance(h, list) else []
        except (TypeError, ValueError):
            return []

    def features_of(self, symbol: str) -> dict[str, Any]:
        if self.db is None:
            return {}
        row = self.db.query_one(
            f"SELECT features FROM {self.prefix}symbol_state WHERE symbol=?",
            (symbol,))
        if not row or not row["features"]:
            return {}
        try:
            return json.loads(row["features"])
        except (TypeError, ValueError):
            return {}

    # ------------------------------------------------------------ transitions
    def transition(
        self, symbol: str, new: str, features: Optional[dict[str, Any]] = None
    ) -> str:
        """Apply + persist the transition; returns the new state. Persistence
        happens BEFORE the strategy sends the corresponding orders."""
        return self._apply(symbol, new, features)

    def _apply(self, symbol: str, new: str, features: Optional[dict[str, Any]]) -> str:
        cur = self.state_of(symbol)
        if new != cur and new not in TRANSITIONS.get(cur, set()):
            raise IllegalTransition(
                f"{symbol}: {cur} -> {new} not allowed (spec 2.12)")
        self._memory[symbol] = new
        self._persist(symbol, new, features)
        return new

    def _persist(self, symbol: str, state: str,
                 features: Optional[dict[str, Any]]) -> None:
        from core.state import now_iso
        now = now_iso()
        snap = features or {}
        if self.db is not None:
            prev = self.db.query_one(
                f"SELECT history FROM {self.prefix}symbol_state WHERE symbol=?",
                (symbol,))
            hist: list[str] = []
            if prev and prev["history"]:
                try:
                    loaded = json.loads(prev["history"])
                    hist = loaded if isinstance(loaded, list) else []
                except (TypeError, ValueError):
                    hist = []
            if not (hist and hist[-1] == state):
                hist = hist + [state]
            self.db.exec(
                f"INSERT INTO {self.prefix}symbol_state (symbol, state, features,"
                " history, updated_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(symbol) DO UPDATE SET state=?, features=?,"
                " history=?, updated_at=?",
                (symbol, state, json.dumps(snap, default=str),
                 json.dumps(hist), now, state,
                 json.dumps(snap, default=str), json.dumps(hist), now),
            )
        if self.events is not None:
            self.events.log(
                "info", f"state {symbol} -> {state}", symbol=(symbol),
                features={"state": state, **snap}, algo=self.algo)
