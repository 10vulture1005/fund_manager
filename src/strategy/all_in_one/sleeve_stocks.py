"""Sleeve STOCKS (spec 2.8): systematic accumulate -> distribute, long-only.

ACCUMULATE (a new lot when NO lots open):
    close <= rolling-30d high * (1 - dd_entry_pct%) AND RSI(14,1D) <= rsi_accumulate
Add lots every further lot_spacing_pct% below the newest lot, up to
stock_max_lots per symbol. Lot size = sleeve budget / (stock_max_lots * n_symbols).

DISTRIBUTE (rebound, momentum fading), on daily closes:
    RSI >= rsi_distribute_floor within the last rebound_window_days days AND
    (RSI < its 3-day SMA OR MACD histogram fell two bars in a row OR
     close < EMA(10) after being above it)
Then SELL the NEWEST lot (LIFO), at most max_lots_sold_per_day per day, only
if the lot is >= min_lot_profit_pct% in profit; else hold. Sell via
reduce-only limit orders near the touch.

Per-symbol stop: down stock_symbol_stop_pct% from average cost -> that symbol
goes DISTRIBUTE_ONLY (no more adds).
Spread filter widened by time-of-day table; optional earnings blackout CSV.
"""
from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

import pandas as pd

from core.indicators import ema, macd, rsi, sma
from core.models import Side, Ticker
from strategy.all_in_one.defaults import get
from strategy.all_in_one.sleeve_base import (
    SleeveBase,
    SleeveStatus,
    new_cid,
    now_utc,
    round_tick,
)


class StocksSleeve(SleeveBase):
    name = "stocks"
    entry_side = Side.BUY

    def __init__(self, exec_port, ledger, events, cfg, store,
                 regime=None, algo: str = "aio", symbols: Optional[list[str]] = None) -> None:
        syms = [str(s).upper() for s in
                (symbols if symbols is not None
                 else (get(cfg, "stocks.stock_symbols", []) or []))]
        if not syms:                       # discovery is done by the strategy
            syms = []
        super().__init__(self.name, syms, exec_port, ledger, events, cfg,
                         store, regime=regime, algo=algo)
        self.symbol_status: dict[str, str] = {s: "ACTIVE" for s in syms}
        self.sold_today: dict[str, tuple[str, int]] = {}   # symbol -> (date, count)
        self._earn_blackout: dict[str, list[str]] = {}
        csvp = self.cfg_get("earnings_blackout_csv", None)
        if csvp:
            try:
                with open(str(csvp)) as fh:
                    for row in csv.reader(fh):
                        if len(row) >= 2 and row[0].strip():
                            sym = row[0].strip().upper()
                            self._earn_blackout.setdefault(sym, []).append(row[1].strip())
            except OSError as e:
                self.event("earnings_csv_missing", str(e), None)

    # ------------------------------------------------------- spread/session
    def spread_cap(self, now: Optional[datetime] = None) -> float:
        base = float(self.cfg_get("spread_base_pct", 0.01))
        table = dict(self.cfg_get("spread_time_table", {}) or {})
        win = list(self.cfg_get("us_market_hours_utc", ["13:30", "20:00"]) or [])
        try:
            h1 = int(win[0][:2]) + int(win[0][3:]) / 60.0
            h2 = int(win[1][:2]) + int(win[1][3:]) / 60.0
        except Exception:
            h1, h2 = 13.5, 20.0
        t = now or now_utc()
        hour = t.hour + t.minute / 60.0
        in_hours = h1 <= hour < h2
        return base * float(table.get("us_market_hours" if in_hours else "us_market_closed", 1.0))

    def earnings_blackout(self, symbol: str, ts: datetime) -> bool:
        dates = self._earn_blackout.get(symbol.upper(), [])
        if not dates:
            return False
        blk = int(self.cfg_get("earnings_blackout_days", 2))
        for d in dates:
            try:
                e = datetime.fromisoformat(d).date()
            except ValueError:
                continue
            delta = abs((ts.date() - e).days)
            if delta <= blk:
                return True
        return False

    # ------------------------------------------------- accumulation triggers
    def dd_from_high(self, symbol: str, price: float) -> Optional[float]:
        """(rolling high - price) / high over dd_lookback_days (0..1+)."""
        look = int(self.cfg_get("dd_lookback_days", 30))
        df = self.store.frame(symbol, "1D")
        if df.empty:
            return None
        high = float(df["high"].astype(float).tail(look).max())
        if high <= 0:
            return None
        return (high - price) / high

    def rsi14(self, symbol: str) -> Optional[float]:
        df = self.store.frame(symbol, "1D")
        if df.empty or len(df) < 16:
            return None
        v = rsi(df["close"].astype(float), 14)
        val = v.iloc[-1]
        return None if pd.isna(val) else float(val)

    def accumulate_ok(self, symbol: str, price: float) -> tuple[bool, dict]:
        """Need dd >= dd_entry_pct AND RSI <= rsi_accumulate (spec 2.8)."""
        need_dd = float(self.cfg_get("dd_entry_pct", 8)) / 100.0
        dd = self.dd_from_high(symbol, price)
        r = self.rsi14(symbol)
        rr = self.cfg_get("rsi_accumulate", 40)
        feats = {"dd": dd, "rsi": r, "need_dd": need_dd, "rsi_max": rr}
        if dd is None or r is None:
            return False, feats
        return (dd >= need_dd and r <= float(rr)), feats

    def spacing_ok(self, symbol: str, price: float) -> bool:
        """Add lots every lot_spacing_pct% below the newest lot."""
        newest = self.newest_lot(symbol)
        if newest is None:
            return False
        sp = float(self.cfg_get("lot_spacing_pct", 5)) / 100.0
        return price <= float(newest["entry_price"]) * (1.0 - sp)

    # ------------------------------------------------- distribution triggers
    def rebound_ok(self, symbol: str) -> tuple[bool, dict]:
        df = self.store.frame(symbol, "1D")
        if df.empty or len(df) < 25:
            return False, {}
        close = df["close"].astype(float)
        r = rsi(close, 14)
        win = int(self.cfg_get("rebound_window_days", 5))
        floorv = float(self.cfg_get("rsi_distribute_floor", 60))
        recent = r.tail(win)
        had_rebound = bool((recent >= floorv).any())
        rsi3 = sma(r, 3)
        cond1 = bool(r.iloc[-1] < rsi3.iloc[-1]) if pd.notna(rsi3.iloc[-1]) else False
        m = macd(close)
        hist = m["histogram"].tail(3).astype(float).tolist()
        cond2 = len(hist) == 3 and hist[-1] < hist[-2] < hist[-3]
        e10 = ema(close, 10)
        cond3 = bool(close.iloc[-1] < e10.iloc[-1]) and bool(
            (close.tail(3).reset_index(drop=True) >= e10.tail(3).reset_index(drop=True)).any()
        )
        feats = {"had_rebound": had_rebound, "rsi_cross_below_sma3": cond1,
                 "macd_hist_two_falling": cond2, "close_below_ema10": cond3}
        return (had_rebound and (cond1 or cond2 or cond3)), feats

    def lot_profitable(self, lot: dict, price: float) -> bool:
        minp = float(self.cfg_get("min_lot_profit_pct", 3)) / 100.0
        entry = float(lot["entry_price"])
        return entry > 0 and (price - entry) / entry >= minp

    # ---------------------------------------------------------- session book
    def _sold_count_today(self, symbol: str, now: datetime) -> int:
        rec = self.sold_today.get(symbol)
        day = now.date().isoformat()
        return rec[1] if rec and rec[0] == day else 0

    def _bump_sold(self, symbol: str, now: datetime) -> None:
        day = now.date().isoformat()
        c = self._sold_count_today(symbol, now)
        self.sold_today[symbol] = (day, c + 1)

    def avg_cost(self, symbol: str) -> Optional[float]:
        lots = self.lots_open(symbol)
        if not lots or sum(int(l["contracts"]) for l in lots) == 0:
            return None
        return sum(float(l["entry_price"]) * int(l["contracts"]) for l in lots) / \
            sum(int(l["contracts"]) for l in lots)

    def symbol_stopped(self, symbol: str, price: float) -> bool:
        avg = self.avg_cost(symbol)
        if avg is None:
            return False
        stop = float(self.cfg_get("stock_symbol_stop_pct", 25)) / 100.0
        return (avg - price) / avg >= stop

    # ------------------------------------------------------------- handlers
    def n_symbols(self) -> int:
        return max(1, len(self.symbols))

    async def on_candle(self, candle) -> None:
        """1D closes carry the accumulate/distribute analysis (spec 2.8)."""
        if candle.resolution != "1D" or candle.symbol not in self.symbols:
            return
        await self._daily_setup(candle, candle.ts)

    async def _daily_setup(self, candle, now: datetime) -> None:
        s = candle.symbol
        # per-symbol stop 25% from average cost -> DISTRIBUTE_ONLY
        if self.symbol_status.get(s, "ACTIVE") == "ACTIVE" and \
                self.symbol_stopped(s, float(candle.close)):
            self.symbol_status[s] = "DISTRIBUTE_ONLY"
            self.event("symbol_stop",
                       f"{s} down >= {self.cfg_get('stock_symbol_stop_pct', 25)}% from avg cost",
                       s, {"avg_cost": self.avg_cost(s)})
        # distribution: sell the newest LIFO lot when the trigger fires
        if self.lots_open(s):
            ok, feats = self.rebound_ok(s)
            if ok:
                await self._distribute_one(s, float(candle.close), now, feats)

    async def _distribute_one(self, symbol: str, price: float, now: datetime,
                              feats: dict) -> None:
        newest = self.newest_lot(symbol)
        if newest is None:
            return
        max_sold = int(self.cfg_get("max_lots_sold_per_day", 1))
        if self._sold_count_today(symbol, now) >= max_sold:
            self.event("sell_skip", "daily sold quota reached", symbol,
                       {"limit": max_sold})
            return
        if not self.lot_profitable(newest, price):
            self.event("sell_skip", f"lot not >= min_lot_profit_pct", symbol,
                       {"entry": float(newest["entry_price"]), "price": price})
            return
        # reduce-only limit near the touch (best bid), else revert to the mark
        tick = self.marks.get(symbol)
        bid = float(tick.best_bid) if tick and tick.best_bid else price
        ack = await self._place_sell_newest(symbol, newest, bid)
        if ack:
            self._bump_sold(symbol, now)
            self.event("distribute_sell",
                       f"momentum fade; sold lot {newest['id']} near the touch",
                       symbol, {"feats": {k: str(v) for k, v in feats.items()}})

    async def _place_sell_newest(self, symbol: str, newest: dict, bid: float) -> bool:
        from core.models import OrderIntent, OrderType, TimeInForce
        p = self.products.get(symbol)
        oid = new_cid(self.algo, symbol, "lifo_sell")
        intent = OrderIntent(
            algo=self.algo, product_symbol=symbol, product_id=getattr(p, "product_id", None),
            side=Side.SELL, size=int(newest["contracts"]), order_type=OrderType.LIMIT,
            limit_price=Decimal(str(round_tick(bid, getattr(p, "tick_size", Decimal("0.01"))))),
            time_in_force=TimeInForce.GTC, reduce_only=True, client_order_id=oid,
            purpose="lifo_sell", reason="momentum fade distribution (LIFO newest)",
            meta={"lot_id": int(newest["id"])},
        )
        self.record_intent(intent, "open")
        self.tp_to_lot[oid] = int(newest["id"])   # route its fill like a lot TP
        res = await self.exec_port.place(intent)
        if not res.accepted:
            self.tp_to_lot.pop(oid, None)
            return False
        self.resting_tp[symbol] = oid
        return True

    async def on_tick(self, symbol: str, ticker: Ticker) -> None:
        if self.halted or self.symbol_status.get(symbol, "ACTIVE") == "DISTRIBUTE_ONLY":
            return
        price = _mark(ticker)
        if price is None:
            return
        if self.earnings_blackout(symbol, now_utc()):
            self.event("entry_skip", "earnings_blackout", symbol)
            return
        lots = self.lots_open(symbol)
        maxl = int(self.cfg_get("stock_max_lots", 5))
        if len(lots) >= maxl:
            return
        if lots:
            if not self.spacing_ok(symbol, price):
                return
        else:
            ok, feats = self.accumulate_ok(symbol, price)
            if not ok:
                return
        await self.request_new_lot(symbol, price, reason="accumulate lot")

    async def request_new_lot(self, symbol: str, price: float,
                              reason: str = "") -> Optional[str]:
        if self.portfolio is None:
            return None
        ok, why = self.portfolio.allow_new_lot(self.name)
        if not ok:
            self.event("entry_skip", why, symbol)
            return None
        budget = self.portfolio.budget_for_new_lot(
            sleeve=self.name,
            max_lots=int(self.cfg_get("stock_max_lots", 5)),
            high_vol=False, n_symbols=self.n_symbols(),
        )
        if budget <= 0:
            self.event("entry_skip", "zero_budget", symbol)
            return None
        return await self.place_new_lot(symbol, budget, price, reason or "accumulate lot")

    def status(self) -> dict[str, Any]:
        out = super().status()
        out["per_symbol_state"] = dict(self.symbol_status)
        return out


def _mark(ticker: Ticker) -> Optional[float]:
    v = ticker.mark_price or ticker.best_bid or ticker.best_ask
    return float(v) if v is not None else None
