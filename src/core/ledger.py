"""LIFO lot ledger — SQLite bookkeeping shared by both strategies (spec 3.3,
prompt B 2.4 / prompt A 2.7).

- Tables are PREFIXABLE: construct with a prefix so two strategies can share
  the code with distinct tables (rwa_positional_Game/lots vs ai_lots, etc.).
- One lot per entry fill; strict-LIFO means only the NEWEST open lot of a
  (sleeve, symbol) may be closed; when it closes the next-newest becomes
  eligible.
- `sum_signed_contracts(symbol)` must equal the exchange net position
  (signed contracts) — any mismatch halts the sleeve (spec 3.3).
- Write-before-send semantics: callers open/persist rows BEFORE the order
  send, then confirm with update calls (crash-safe, spec 3.3).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .state import DeltaSchema, now_iso

LOT_SCHEMA = """\
CREATE TABLE IF NOT EXISTS {p}lots (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 sleeve TEXT NOT NULL,
 symbol TEXT NOT NULL,
 side TEXT NOT NULL,               -- buy|sell (signed: buy=+1, sell=-1)
 contracts INTEGER NOT NULL,
 entry_price REAL,
 opened_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open',  -- open|closed
 close_price REAL,
 closed_at TEXT,
 entry_client_oid TEXT,
 exit_client_oid TEXT,
 tp_price REAL,
 confirmed INTEGER NOT NULL DEFAULT 0, -- entry-fill confirmation (3.3)
 meta TEXT
)"""

HEDGE_SCHEMA = """\
CREATE TABLE IF NOT EXISTS {p}hedges (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 sleeve TEXT NOT NULL,
 option_symbol TEXT NOT NULL,
 product_id INTEGER,
 side TEXT NOT NULL,
 contracts INTEGER NOT NULL,
 avg_price REAL,
 opened_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open',  -- open|closed
 close_price REAL,
 closed_at TEXT,
 entry_client_oid TEXT,
 exit_client_oid TEXT,
 meta TEXT
)"""

ORDER_SCHEMA = """\
CREATE TABLE IF NOT EXISTS {p}orders (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 client_order_id TEXT,
 exchange_order_id TEXT,
 symbol TEXT NOT NULL,
 side TEXT,
 size INTEGER,
 filled_size INTEGER DEFAULT 0,
 avg_fill_price REAL,
 status TEXT DEFAULT 'new',
 order_type TEXT,
 limit_price REAL,
 reduce_only INTEGER DEFAULT 0,
 product_id INTEGER,
 ts TEXT NOT NULL,
 meta TEXT
)"""

CAMPAIGN_SCHEMA = """\
CREATE TABLE IF NOT EXISTS {p}campaigns (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 symbol TEXT NOT NULL,
 direction TEXT NOT NULL,
 state TEXT NOT NULL,             -- IDLE|ARMED|TRIGGERED|BUILDING|MANAGING|EXITING|CLOSED|HALTED (2.12)
 zone TEXT,                       -- json zone/level snapshot
 opened_at TEXT,
 closed_at TEXT,
 close_reason TEXT,
 entry_avg REAL,
 contracts INTEGER,
 tp_price REAL,
 meta TEXT
)"""


def _sign(side: str) -> int:
    side_l = (side or "").lower()
    if side_l in ("buy", "long", "b"):
        return 1
    if side_l in ("sell", "short", "s"):
        return -1
    raise ValueError(f"unknown lot side {side!r}")


class LedgerError(RuntimeError):
    """Violated LIFO invariant / unknown row / state machine misuse."""


class LIFOLedger:
    """Lot/hedge/order/campaign bookkeeping over a caller-supplied db path."""

    def __init__(self, db_path: str, prefix: str = "") -> None:
        self.db = DeltaSchema(db_path, table_prefix=prefix)
        self.init()

    def init(self) -> None:
        p = self.db.table_prefix
        self.db.exec(LOT_SCHEMA.format(p=p))
        self.db.exec(HEDGE_SCHEMA.format(p=p))
        self.db.exec(ORDER_SCHEMA.format(p=p))
        self.db.exec(CAMPAIGN_SCHEMA.format(p=p))

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        d = dict(row)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except (TypeError, ValueError):
            d["meta"] = {}
        return d

    # ------------------------------------------------------------------ lots
    def open_lot(
        self,
        sleeve: str,
        symbol: str,
        side: str,
        contracts: int,
        entry_price: float | None,
        opened_at: str | None = None,
        entry_client_oid: str = "",
        tp_price: float | None = None,
        confirmed: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Insert an open lot BEFORE the entry order is sent (write-before-send,
        spec 3.3). Call `confirm_lot_entry` after the entry fill confirmation.
        """
        if contracts <= 0:
            raise LedgerError("lot contracts must be positive")
        _sign(side)  # validates
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"INSERT INTO {self.db.table_prefix}lots"
                " (sleeve, symbol, side, contracts, entry_price, opened_at, status,"
                "  entry_client_oid, tp_price, confirmed, meta)"
                " VALUES (?,?,?,?,?,?,'open',?,?,?,?)",
                (
                    sleeve, symbol, side.lower(), int(contracts), entry_price,
                    opened_at or now_iso(), entry_client_oid, tp_price,
                    1 if confirmed else 0,
                    json.dumps(meta or {}, default=str),
                ),
            )
        return int(cur.lastrowid or 0)

    def confirm_lot_entry(
        self, lot_id: int, entry_client_oid: str | None = None,
        entry_price: float | None = None,
    ) -> None:
        """Entry fill confirmation: mark confirmed + set the exchange oid (3.3)."""
        row = self.lot(lot_id)
        if row is None:
            raise LedgerError(f"lot {lot_id} not found")
        oid = entry_client_oid if entry_client_oid is not None else row["entry_client_oid"]
        price = entry_price if entry_price is not None else row["entry_price"]
        self.db.exec(
            f"UPDATE {self.db.table_prefix}lots SET confirmed=1,"
            " entry_client_oid=COALESCE(?, entry_client_oid),"
            " entry_price=COALESCE(?, entry_price) WHERE id=?",
            (oid, price, lot_id),
        )

    def close_lot(
        self,
        lot_id: int,
        price: float,
        ts: str | None = None,
        exit_client_oid: str = "",
        enforce_strict_lifo: bool = True,
    ) -> None:
        """Close a lot; in strict mode lot_id must be the NEWEST open one (2.4).

        Returns nothing; raises LedgerError on LIFO violation and on closing
        an already-closed or unknown lot. When the newest lot closes, the
        next-newest automatically becomes eligible — nothing to do here.
        """
        row = self.lot(lot_id)
        if row is None:
            raise LedgerError(f"lot {lot_id} not found")
        if row["status"] != "open":
            raise LedgerError(f"lot {lot_id} already {row['status']}")
        if enforce_strict_lifo:
            newest = self.newest_open_lot(row["sleeve"], row["symbol"])
            if newest is None or newest["id"] != lot_id:
                raise LedgerError(
                    f"strict LIFO: lot {lot_id} is not the newest open lot "
                    f"(newest is {(newest or {}).get('id')})"
                )
        self.db.exec(
            f"UPDATE {self.db.table_prefix}lots SET status='closed', close_price=?,"
            " closed_at=?, exit_client_oid=COALESCE(NULLIF(?,''), exit_client_oid)"
            " WHERE id=?",
            (price, ts or now_iso(), exit_client_oid, lot_id),
        )

    def lot(self, lot_id: int) -> dict[str, Any] | None:
        rows = self.db.query(
            f"SELECT * FROM {self.db.table_prefix}lots WHERE id=?", (lot_id,)
        )
        return self._row_to_dict(rows[0]) if rows else None

    def newest_open_lot(self, sleeve: str, symbol: str) -> dict[str, Any] | None:
        """The single lot that strict LIFO allows to close (prompt B 2.4)."""
        rows = self.db.query(
            f"SELECT * FROM {self.db.table_prefix}lots"
            " WHERE sleeve=? AND symbol=? AND status='open'"
            " ORDER BY id DESC LIMIT 1",
            (sleeve, symbol),
        )
        return self._row_to_dict(rows[0]) if rows else None

    def open_lots(self, sleeve: str | None = None, symbol: str | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {self.db.table_prefix}lots WHERE status='open'"
        params: list[Any] = []
        if sleeve is not None:
            sql += " AND sleeve=?"
            params.append(sleeve)
        if symbol is not None:
            sql += " AND symbol=?"
            params.append(symbol)
        rows = self.db.query(sql + " ORDER BY id ASC", params)
        return [self._row_to_dict(r) for r in rows]

    def sum_signed_contracts(self, symbol: str, sleeve: str | None = None) -> int:
        """Sum of open lots (signed) per symbol == exchange position (2.4)."""
        return sum(
            _sign(l["side"]) * int(l["contracts"])
            for l in self.open_lots(sleeve=sleeve, symbol=symbol)
        )

    # ---------------------------------------------------------------- hedges
    def open_hedge(
        self,
        sleeve: str,
        option_symbol: str,
        product_id: int | None,
        side: str,
        contracts: int,
        avg_price: float | None,
        opened_at: str | None = None,
        entry_client_oid: str = "",
        meta: dict[str, Any] | None = None,
    ) -> int:
        _sign(side)
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"INSERT INTO {self.db.table_prefix}hedges"
                " (sleeve, option_symbol, product_id, side, contracts, avg_price,"
                "  opened_at, status, entry_client_oid, meta)"
                " VALUES (?,?,?,?,?,?,?,'open',?,?)",
                (
                    sleeve, option_symbol, product_id, side.lower(), int(contracts),
                    avg_price, opened_at or now_iso(), entry_client_oid,
                    json.dumps(meta or {}, default=str),
                ),
            )
        return int(cur.lastrowid or 0)

    def close_hedge(
        self, hedge_id: int, price: float, ts: str | None = None,
        exit_client_oid: str = "",
    ) -> None:
        row = self.db.query_one(
            f"SELECT status FROM {self.db.table_prefix}hedges WHERE id=?", (hedge_id,)
        )
        if row is None:
            raise LedgerError(f"hedge {hedge_id} not found")
        if row["status"] != "open":
            raise LedgerError(f"hedge {hedge_id} already {row['status']}")
        self.db.exec(
            f"UPDATE {self.db.table_prefix}hedges SET status='closed', close_price=?,"
            " closed_at=?, exit_client_oid=COALESCE(NULLIF(?,''), exit_client_oid)"
            " WHERE id=?",
            (price, ts or now_iso(), exit_client_oid, hedge_id),
        )

    def open_hedges(self, sleeve: str | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {self.db.table_prefix}hedges WHERE status='open'"
        params: list[Any] = []
        if sleeve is not None:
            sql += " AND sleeve=?"
            params.append(sleeve)
        rows = self.db.query(sql + " ORDER BY id ASC", params)
        return [self._row_to_dict(r) for r in rows]

    # ---------------------------------------------------------------- orders
    def record_order(
        self,
        client_order_id: str = "",
        exchange_order_id: str = "",
        symbol: str = "",
        side: str = "",
        size: int = 0,
        filled_size: int = 0,
        avg_fill_price: float | None = None,
        status: str = "new",
        order_type: str = "",
        limit_price: float | None = None,
        reduce_only: bool = False,
        product_id: int | None = None,
        ts: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Upsert an order row keyed by client_order_id (idempotent on retry)."""
        if client_order_id:
            existing = self.db.query_one(
                f"SELECT id FROM {self.db.table_prefix}orders WHERE client_order_id=?",
                (client_order_id,),
            )
            if existing:
                self.db.exec(
                    f"UPDATE {self.db.table_prefix}orders SET"
                    " exchange_order_id=COALESCE(NULLIF(?,''), exchange_order_id),"
                    " filled_size=?, avg_fill_price=COALESCE(?, avg_fill_price),"
                    " status=COALESCE(NULLIF(?,''), status), ts=?"
                    " WHERE id=?",
                    (
                        exchange_order_id, filled_size, avg_fill_price, status,
                        ts or now_iso(), existing["id"],
                    ),
                )
                return int(existing["id"])
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"INSERT INTO {self.db.table_prefix}orders"
                " (client_order_id, exchange_order_id, symbol, side, size, filled_size,"
                "  avg_fill_price, status, order_type, limit_price, reduce_only,"
                "  product_id, ts, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    client_order_id, exchange_order_id, symbol, side, size, filled_size,
                    avg_fill_price, status, order_type, limit_price,
                    1 if reduce_only else 0, product_id, ts or now_iso(),
                    json.dumps(meta or {}, default=str),
                ),
            )
        return int(cur.lastrowid or 0)

    def list_orders(
        self, symbol: str | None = None, status: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {self.db.table_prefix}orders"
        conds, params = [], []
        if symbol:
            conds.append("symbol=?")
            params.append(symbol)
        if status:
            conds.append("status=?")
            params.append(status)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += f" ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [self._row_to_dict(r) for r in self.db.query(sql, params)]

    def get_order(self, client_order_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            f"SELECT * FROM {self.db.table_prefix}orders WHERE client_order_id=?",
            (client_order_id,),
        )
        return self._row_to_dict(row) if row else None

    # -------------------------------------------------------------- campaigns
    def create_campaign(
        self,
        symbol: str,
        direction: str,
        state: str,
        opened_at: str | None = None,
        zone: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> int:
        """Generic campaign row (rwa_positional's scale-in ladder, spec 2.7/2.12)."""
        with self.db.transaction() as conn:
            cur = conn.execute(
                f"INSERT INTO {self.db.table_prefix}campaigns"
                " (symbol, direction, state, zone, opened_at, meta)"
                " VALUES (?,?,?,?,?,?)",
                (
                    symbol, direction, state,
                    json.dumps(zone or {}, default=str),
                    opened_at or now_iso(),
                    json.dumps(meta or {}, default=str),
                ),
            )
        return int(cur.lastrowid or 0)

    def update_campaign(
        self,
        campaign_id: int,
        state: str | None = None,
        entry_avg: float | None = None,
        contracts: int | None = None,
        tp_price: float | None = None,
        zone: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        sets, params = [], []
        if state is not None:
            sets.append("state=?")
            params.append(state)
        if entry_avg is not None:
            sets.append("entry_avg=?")
            params.append(entry_avg)
        if contracts is not None:
            sets.append("contracts=?")
            params.append(contracts)
        if tp_price is not None:
            sets.append("tp_price=?")
            params.append(tp_price)
        if zone is not None:
            sets.append("zone=?")
            params.append(json.dumps(zone, default=str))
        if meta is not None:
            sets.append("meta=?")
            params.append(json.dumps(meta, default=str))
        if not sets:
            return
        self.db.exec(
            f"UPDATE {self.db.table_prefix}campaigns SET {', '.join(sets)} WHERE id=?",
            (*params, campaign_id),
        )

    def update_state(self, campaign_id: int, state: str) -> None:
        """Explicit alias used by the state machine (spec 2.12)."""
        self.update_campaign(campaign_id, state=state)

    def get_campaigns(
        self, symbol: str | None = None, state: str | None = None
    ) -> list[dict[str, Any]]:
        sql = f"SELECT * FROM {self.db.table_prefix}campaigns"
        conds, params = [], []
        if symbol is not None:
            conds.append("symbol=?")
            params.append(symbol)
        if state is not None:
            conds.append("state=?")
            params.append(state)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        rows = self.db.query(sql + " ORDER BY id DESC", params)
        out = []
        for r in rows:
            d = dict(r)
            d["zone"] = json.loads(d.get("zone") or "{}")
            d["meta"] = json.loads(d.get("meta") or "{}")
            out.append(d)
        return out

    def close_campaign(
        self, campaign_id: int, close_reason: str = "", ts: str | None = None,
    ) -> None:
        self.db.exec(
            f"UPDATE {self.db.table_prefix}campaigns"
            " SET state='CLOSED', close_reason=?, closed_at=? WHERE id=?",
            (close_reason, ts or now_iso(), campaign_id),
        )

    def close(self) -> None:
        self.db.close()
