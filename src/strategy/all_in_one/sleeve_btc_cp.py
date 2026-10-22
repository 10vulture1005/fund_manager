"""Sleeve BTC_CP (spec 2.7): mirror of ETH_CC with short bias.

Long-side mirror: SHORT BTCUSD perp lots + SHORT put options covering the
(short) coin exposure. Add on rallies: mark >= newest_entry + spacing*ATR.
Strict-LIFO cover (buy to close) at lot_entry * (1 - lot_tp_pct%).
Lower caps: max_lots 4, leverage cap 2. No sleeve execution changes beyond
direction (only ACTIVE regimes pass the 2.9 map).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from core.models import Side, Ticker
from strategy.all_in_one.defaults import get
from strategy.all_in_one.sleeve_base import SleeveBase, SleeveStatus, now_utc


class BtcCpSleeve(SleeveBase):
    name = "btc_cp"
    entry_side = Side.SELL

    def __init__(self, exec_port, ledger, events, cfg, store,
                 regime=None, algo: str = "aio", symbols: Optional[list[str]] = None) -> None:
        sym = str(get(cfg, "btc_cp.symbol", "BTCUSD")) if not symbols else symbols[0]
        super().__init__(self.name, [sym], exec_port, ledger, events, cfg,
                         store, regime=regime, algo=algo)
        self._derisked = 0

    async def warmup(self, now: Optional[datetime] = None) -> None:
        await self.load_products()
        await self.setup_options_hedge()

    def _hedge_mode(self, symbol: str) -> Optional[str]:
        return self.hedge_kind_for(symbol, "put")

    def should_open_first_lot(self, symbol: str) -> bool:
        return self.status is SleeveStatus.ACTIVE

    def should_add_lot(self, symbol: str, price: float) -> Optional[float]:
        """Add on rallies: price >= newest_entry + lot_spacing_atr * ATR14."""
        newest = self.newest_lot(symbol)
        if newest is None or self.status is not SleeveStatus.ACTIVE:
            return None
        spacing = float(self.cfg_get("lot_spacing_atr", 1.0))
        a = self.atr(symbol)
        if a is None or a <= 0:
            return None
        if price >= float(newest["entry_price"]) + spacing * a:
            return a
        return None

    async def on_tick(self, symbol: str, ticker: Ticker) -> None:
        if self.halted or self.status is SleeveStatus.PAUSED:
            return
        price = _mark(ticker)
        if price is None:
            return
        lots = self.lots_open(symbol)
        if len(lots) >= self.max_lots(symbol):
            return
        if lots:
            if self.should_add_lot(symbol, price) is None:
                return
        else:
            if not self.should_open_first_lot(symbol):
                return
        await self.request_new_lot(symbol, price, reason="covered-put lot")

    async def request_new_lot(self, symbol: str, price: float,
                              reason: str = "") -> Optional[str]:
        if self.portfolio is None:
            return None
        ok, why = self.portfolio.allow_new_lot(self.name)
        if not ok:
            self.event("entry_skip", why, symbol)
            return None
        high_vol = False
        if self.regime is not None:
            snap = self.regime.get(symbol)
            high_vol = bool(snap.high_vol) if snap else False
        budget = self.portfolio.budget_for_new_lot(
            sleeve=self.name, max_lots=self.max_lots(symbol), high_vol=high_vol,
            n_symbols=1,
        )
        if budget <= 0:
            self.event("entry_skip", "zero_budget", symbol)
            return None
        return await self.place_new_lot(symbol, budget, price, reason or "new lot")

    async def on_timer(self, now: Optional[datetime] = None) -> None:
        if self.halted:
            return
        stop_pct = float(self.cfg_get("sleeve_stop_pct", 15)) / 100.0
        steps = int(self.cfg_get("derisk_steps", 3))
        for s in self.symbols:
            est = self.realized_pnl(s)
            hwm = self.sleeve_hwm.get(s)
            if hwm is not None:
                self.sleeve_hwm[s] = max(hwm, est)
            if (
                self.status is SleeveStatus.ACTIVE
                and hwm is not None and hwm > 0 and est < 0
                and abs(est) / abs(hwm) >= stop_pct
            ):
                dd = abs(est) / abs(hwm)
                self.set_status(
                    SleeveStatus.DISTRIBUTE_ONLY.value,
                    f"sleeve stop: drawdown {dd:.2%} >= sleeve_stop_pct",
                )
                self._derisked = 0
            if self.status is SleeveStatus.DISTRIBUTE_ONLY and self.lots_open(s):
                if self._derisked < steps:
                    closed = await self.derisk_step(now or now_utc())
                    self._derisked += len(closed)
                    if closed:
                        self.event("derisk", f"closed lots {closed} (newest-first)", s)
                    if not self.lots_open(s):
                        nxt = (self.regime.sleeve_status("BTC_CP", s)
                               if self.regime is not None else "PAUSED")
                        self.set_status(nxt, "derisk complete")


def _mark(ticker: Ticker) -> Optional[float]:
    v = ticker.mark_price or ticker.best_bid or ticker.best_ask
    return float(v) if v is not None else None
