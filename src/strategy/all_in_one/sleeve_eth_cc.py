"""Sleeve ETH_CC (spec 2.6): long ETHUSD perp lots + SHORT call options.

- First lot when the sleeve is ACTIVE and the regime filter passes (2.9).
- Further lots on dips: mark <= newest_entry - lot_spacing_atr * ATR(14,1D),
  up to max_lots (5).
- Lot size = sleeve budget / max_lots; sleeve leverage cap 3.
- Strict-LIFO exit: newest lot TP = entry * (1 + lot_tp_pct%) (one resting
  reduce-only limit for the newest lot, software mark-price fallback).
- Sleeve stop: -sleeve_stop_pct% vs the sleeve high-water -> DISTRIBUTE_ONLY,
  close newest lots first over derisk_steps steps.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from core.models import Side, Ticker
from strategy.all_in_one.defaults import get
from strategy.all_in_one.sleeve_base import SleeveBase, SleeveStatus, now_utc


class EthCcSleeve(SleeveBase):
    name = "eth_cc"
    entry_side = Side.BUY

    def __init__(self, exec_port, ledger, events, cfg, store,
                 regime=None, algo: str = "aio", symbols: Optional[list[str]] = None) -> None:
        sym = str(get(cfg, "eth_cc.symbol", "ETHUSD")) if not symbols else symbols[0]
        super().__init__(self.name, [sym], exec_port, ledger, events, cfg,
                         store, regime=regime, algo=algo)
        self._derisked = 0

    async def warmup(self, now: Optional[datetime] = None) -> None:
        await self.load_products()
        await self.setup_options_hedge()

    def _hedge_mode(self, symbol: str) -> Optional[str]:
        return self.hedge_kind_for(symbol, "call")

    # ------------------------------------------------------- entry triggers
    def should_open_first_lot(self, symbol: str) -> bool:
        """First lot only when the sleeve is ACTIVE (regime passes, 2.9)."""
        return self.status is SleeveStatus.ACTIVE

    def should_add_lot(self, symbol: str, price: float) -> Optional[float]:
        """Add on dips: price <= newest_entry - lot_spacing_atr * ATR(14,1D).
        Returns the ATR used, or None when not triggered / not allowed."""
        newest = self.newest_lot(symbol)
        if newest is None or self.status is not SleeveStatus.ACTIVE:
            return None
        spacing = float(self.cfg_get("lot_spacing_atr", 1.0))
        a = self.atr(symbol)
        if a is None or a <= 0:
            return None
        if price <= float(newest["entry_price"]) - spacing * a:
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
            trig = self.should_add_lot(symbol, price)
            if trig is None:
                return
        else:
            if not self.should_open_first_lot(symbol):
                return
        await self.request_new_lot(symbol, price, reason="covered-call lot")

    async def request_new_lot(self, symbol: str, price: float,
                              reason: str = "") -> Optional[str]:
        """Portfolio-gated lot entry (budget / breaches / margin mode)."""
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

    # ------------------------------------------------------------ sleeve stop
    async def on_timer(self, now: Optional[datetime] = None) -> None:
        """Sleeve-level stop (spec 2.6): equity attributable to the sleeve
        falls sleeve_stop_pct% below its high-water -> DISTRIBUTE_ONLY and
        close newest lots first over derisk_steps steps."""
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
                        nxt = (self.regime.sleeve_status("ETH_CC", s)
                               if self.regime is not None else "PAUSED")
                        self.set_status(nxt, "derisk complete")


def _mark(ticker: Ticker) -> Optional[float]:
    v = ticker.mark_price or ticker.best_bid or ticker.best_ask
    return float(v) if v is not None else None
