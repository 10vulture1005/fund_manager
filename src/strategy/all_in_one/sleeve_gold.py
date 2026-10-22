"""Sleeve GOLD (spec 2.8): XAUTUSD + PAXGUSD with CC_MODE / CP_MODE.

- Mode per symbol by regime (2.9): CC_MODE (long lots + short calls) in
  neutral/up regimes, CP_MODE (short lots + short puts) in neutral/down.
- A symbol may switch mode ONLY when its lot stack is empty AND the regime
  flip has persisted `mode_switch_days` (3) days.
- Same lot-spacing / strict-LIFO TP / hedge logic as 2.6/2.7 but with
  gold-appropriate defaults (lot_spacing_atr 0.8, lot_tp_pct 1.0, cap 3).
- XAUT and PAXG share ONE risk bucket: combined open lots <= gold.max_lots.
- No options for RWA (spec 2.5) -> hedge_mode none, logged at setup.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from core.models import Side, Ticker
from strategy.all_in_one.defaults import get
from strategy.all_in_one.sleeve_base import SleeveBase, SleeveStatus, now_utc

CC_MODE = "CC_MODE"
CP_MODE = "CP_MODE"


class GoldSleeve(SleeveBase):
    name = "gold"

    def __init__(self, exec_port, ledger, events, cfg, store,
                 regime=None, algo: str = "aio", symbols: Optional[list[str]] = None) -> None:
        syms = [str(s).upper() for s in
                (symbols if symbols is not None
                 else (get(cfg, "gold.symbols", ["XAUTUSD", "PAXGUSD"]) or []))]
        super().__init__(self.name, syms, exec_port, ledger, events, cfg,
                         store, regime=regime, algo=algo)
        self.mode: dict[str, str] = {}            # per symbol: CC_MODE / CP_MODE
        self._flip_count: dict[str, int] = {}     # persisted regime-flip days
        self._flip_candidate: dict[str, Optional[str]] = {}
        self._derisked = 0

    async def warmup(self, now: Optional[datetime] = None) -> None:
        await self.load_products()
        await self.setup_options_hedge()   # likely hedge_mode none for XAUT/PAXG

    # ------------------------------------------------------------- modes
    def lots_total(self) -> int:
        return sum(len(self.lots_open(s)) for s in self.symbols)

    def entry_side_for(self, symbol: str) -> Side:
        mode = self.mode.get(symbol)
        if mode == CP_MODE:
            return Side.SELL
        return Side.BUY

    def _hedge_mode(self, symbol: str) -> Optional[str]:
        mode = self.mode.get(symbol)
        if mode == CC_MODE:
            return self.hedge_kind_for(symbol, "call")
        if mode == CP_MODE:
            return self.hedge_kind_for(symbol, "put")
        return None

    def gold_status_map(self, symbol: str) -> str:
        """Sleeve status by regime for the symbol's CURRENT mode (spec 2.9)."""
        if self.regime is None:
            return self.status.value
        snap = self.regime.get(symbol)
        if snap is None:
            return "PAUSED"
        key = "gold_cc_map" if self.mode.get(symbol, CC_MODE) == CC_MODE else "gold_cp_map"
        from strategy.all_in_one.regime import trend_to_status
        mapping = dict(get(self.cfg, f"regime.{key}", {}) or {})
        if not mapping:     # spec defaults: CC active in neutral/up, CP active in neutral/down
            mapping = ({"strong_down": "PAUSED", "down": "PAUSED", "neutral": "ACTIVE",
                        "up": "ACTIVE", "strong_up": "ACTIVE"} if key == "gold_cc_map"
                       else {"strong_down": "ACTIVE", "down": "ACTIVE", "neutral": "ACTIVE",
                             "up": "PAUSED", "strong_up": "PAUSED"})
        return trend_to_status(mapping, snap.trend)

    def candidate_mode(self, symbol: str) -> Optional[str]:
        """Mode the regime currently calls for ('CC_MODE' | 'CP_MODE')."""
        if self.regime is None:
            return None
        snap = self.regime.get(symbol)
        if snap is None:
            return None
        return self.regime.gold_mode(symbol)

    def on_daily_close(self, symbol: str, now: datetime) -> None:
        """Daily regime job: mode switching with persistence + empty stack."""
        cand = self.candidate_mode(symbol)
        cur = self.mode.get(symbol)
        if cand is None:
            return
        if cur is None:
            self.mode[symbol] = cand                  # first regime observation
            self._flip_candidate[symbol] = cand
            self._flip_count[symbol] = 0
            return
        if cand == cur:
            self._flip_count[symbol] = 0
            self._flip_candidate[symbol] = cur
            self.set_status(self.gold_status_map(symbol), f"{symbol} mode {cur}")
            return
        # regime calls for the other mode: count persistence, switch only when
        # allowed (stack empty, flip persisted) — spec 2.8
        streak = self._flip_count.get(symbol, 0) + 1 if self._flip_candidate.get(symbol) == cand else 1
        self._flip_candidate[symbol] = cand
        self._flip_count[symbol] = streak
        need = int(self.cfg_get("mode_switch_days", 3))
        if streak >= need and len(self.lots_open(symbol)) == 0:
            self.event("mode_switch",
                       f"{symbol}: {cur} -> {cand} (flip persisted {streak}d)",
                       symbol, {"streak": streak}, ev="info")
            self.mode[symbol] = cand
            self._flip_count[symbol] = 0
            self.set_status(self.gold_status_map(symbol), f"{symbol} mode -> {cand}")

    def switched_symbol(self, symbol: str) -> bool:
        """True once the symbol has a mode (post first daily regime job)."""
        return symbol in self.mode

    # ------------------------------------------------------- entry triggers
    def should_add_lot(self, symbol: str, price: float) -> Optional[float]:
        """Same spacing trigger as 2.6/2.7 in the symbol's current mode:
        long lots add on dips, short lots add on rallies."""
        mode = self.mode.get(symbol)
        newest = self.newest_lot(symbol)
        if newest is None or mode is None:
            return None
        if self.status is not SleeveStatus.ACTIVE:
            return None
        spacing = float(self.cfg_get("lot_spacing_atr", 0.8))
        a = self.atr(symbol)
        if a is None or a <= 0:
            return None
        entry = float(newest["entry_price"])
        if mode == CC_MODE and price <= entry - spacing * a:
            return a
        if mode == CP_MODE and price >= entry + spacing * a:
            return a
        return None

    def lots_for_symbol_capped(self, symbol: str) -> bool:
        """Shared bucket: XAUT + PAXG open lots combined <= gold.max_lots."""
        return self.lots_total() >= self.max_lots(symbol)

    async def on_tick(self, symbol: str, ticker: Ticker) -> None:
        if self.halted or self.status is SleeveStatus.PAUSED:
            return
        price = _mark(ticker)
        if price is None:
            return
        if self.mode.get(symbol) is None or self.lots_for_symbol_capped(symbol):
            return
        lots = self.lots_open(symbol)
        if lots:
            if self.should_add_lot(symbol, price) is None:
                return
            if str(lots[-1]["side"]) != self.entry_side_for(symbol).value:
                return   # do not stack against the symbol's current mode
        else:
            if self.status is not SleeveStatus.ACTIVE:
                return
        await self.request_new_lot(symbol, price, reason=f"gold {self.mode.get(symbol)} lot")

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
        return await self.place_new_lot(symbol, budget, price, reason or "gold lot")

    async def on_timer(self, now: Optional[datetime] = None) -> None:
        if self.halted:
            return
        stop_pct = float(self.cfg_get("sleeve_stop_pct", 15)) / 100.0
        steps = int(self.cfg_get("derisk_steps", 3))
        est = sum(self.realized_pnl(s) for s in self.symbols)
        hwm = self.sleeve_hwm.get(self.symbols[0])
        if hwm is not None:
            for s in self.symbols:
                self.sleeve_hwm[s] = max(self.sleeve_hwm.get(s, est), est)
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
        if self.status is SleeveStatus.DISTRIBUTE_ONLY:
            if self.lots_total() and self._derisked < steps:
                closed = await self.derisk_step(now or now_utc())
                self._derisked += len(closed)
                if closed:
                    self.event("derisk", f"closed lots {closed} (newest-first)", None)
                if not self.lots_total():
                    nxt = (self.gold_status_map(self.symbols[0])
                           if self.regime is not None else "PAUSED")
                    self.set_status(nxt, "derisk complete")

    def status(self) -> dict[str, Any]:
        out = super().status()
        out["modes"] = dict(self.mode)
        out["shared_lots"] = self.lots_total()
        out["max_lots_shared"] = self.max_lots(self.symbols[0])
        return out


def _mark(ticker: Ticker) -> Optional[float]:
    v = ticker.mark_price or ticker.best_bid or ticker.best_ask
    return float(v) if v is not None else None
