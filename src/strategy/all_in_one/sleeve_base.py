"""SleeveBase — shared mechanics for the four all_in_one sleeves (spec 2.2).

Every sleeve:
- implements on_candle_close / on_ticker / on_fill / on_timer / status()
- has a status in {ACTIVE, ACCUMULATE_ONLY, DISTRIBUTE_ONLY, PAUSED}
- opens/closes LOTS via the shared LIFOLedger; strict LIFO (spec 2.4):
  only the NEWEST open lot of a (sleeve, symbol) may close
- keeps exactly ONE resting reduce-only limit TP for the newest lot
  (re-placed when a prior lot closes), with a software mark-price fallback
- NEVER touches the exchange directly; all orders go through ExecutionPort.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

import pandas as pd

from core.ledger import LIFOLedger
from core.models import (
    Candle,
    FillEvent,
    OrderIntent,
    OrderType,
    ProductInfo,
    Side,
    Ticker,
    TimeInForce,
)
from strategy.all_in_one.defaults import get
from strategy.all_in_one.options import OptionChain, OptionHedgeManager


class SleeveStatus(str, Enum):
    ACTIVE = "ACTIVE"
    ACCUMULATE_ONLY = "ACCUMULATE_ONLY"
    DISTRIBUTE_ONLY = "DISTRIBUTE_ONLY"
    PAUSED = "PAUSED"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


def round_tick(price: float, tick: Decimal | float) -> float:
    t = float(tick)
    if t <= 0:
        return price
    return round(price / t) * t


def lot_tp_price(side: str, entry: float, tp_pct: float) -> float:
    """Lot TP: long lots -> entry*(1+tp%), short lots -> entry*(1-tp%) —
    the exit trades back toward entry."""
    if side in ("buy", "long"):
        return entry * (1.0 + tp_pct / 100.0)
    return entry * (1.0 - tp_pct / 100.0)


def realized_pnl_of(lot: dict, close_price: float) -> float:
    sign = 1.0 if str(lot["side"]) == "buy" else -1.0
    return sign * (float(close_price) - float(lot["entry_price"])) * float(lot["contracts"])


def new_cid(algo: str, symbol: str, purpose: str) -> str:
    import uuid as _u

    clean = lambda s: s.replace("_", "").replace("-", "").lower()  # noqa: E731
    return (
        f"{clean(algo)}-{clean(symbol).replace('usd', '')}-"
        f"{clean(purpose)}-{_u.uuid4().hex[:8]}"
    )[:64]


class SleeveBase:
    name = "sleeve"
    entry_side = Side.BUY   # class default; per-symbol overrides via _entry_side

    def __init__(
        self,
        name: str,
        symbols: list[str],
        exec_port: Any,
        ledger: LIFOLedger,
        events: Any,                    # core.events.EventLogger
        cfg: dict,                      # the MERGED all_in_one config tree
        store: Any,                     # regime.HistoryStore (shared)
        regime: Any = None,
        algo: str = "aio",
    ) -> None:
        self.name = name
        self.symbols = list(symbols)
        self.exec_port = exec_port
        self.ledger = ledger
        self.events = events
        self.cfg = cfg
        self.store = store
        self.regime = regime
        self.algo = algo
        self.status = SleeveStatus.PAUSED
        self.status_reason = "awaiting_regime"
        self.halted = False
        self.halt_reason: Optional[str] = None
        self.products: dict[str, ProductInfo] = {}
        self.marks: dict[str, Ticker] = {}
        self._entry_side: dict[str, Side] = {s: self.entry_side for s in self.symbols}
        self.portfolio: Any = None
        self.chain = OptionChain(exec_port, cfg)
        self.hedge: Optional[OptionHedgeManager] = None
        # in-memory bookkeeping maps (sqlite stays authoritative for lots)
        self.pending_lots: dict[str, dict] = {}    # entry oid -> lot intent data
        self.resting_tp: dict[str, str] = {}       # symbol -> resting lot-TP oid
        self.tp_to_lot: dict[str, int] = {}        # lot-TP/sell oid -> lot id
        self.market_exits: dict[str, int] = {}     # market-exit oid -> lot id
        self.sleeve_pnl: dict[str, float] = {}
        self.sleeve_hwm: dict[str, float] = {}

    # ------------------------------------------------------------- plumbing
    async def load_products(self) -> None:
        for s in self.symbols:
            p = await self.exec_port.product(s)
            if p is not None and p.trading_status == "operational":
                self.products[s] = p

    def cfg_get(self, dotted: str, default: Any = None) -> Any:
        return get(self.cfg, f"{self.name}.{dotted}", default)

    def event(self, kind: str, message: str, symbol: Optional[str] = None,
              features: Optional[dict] = None) -> None:
        try:
            from core.models import EventNote
            self.events.log_note(EventNote(
                algo=f"{self.algo}.{self.name}", kind=str(kind), symbol=symbol,
                message=message, features=features or {},
            ))
        except Exception:
            pass

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason
        self.event("halt", reason)

    def set_status(self, status: str, reason: str = "") -> bool:
        """Set sleeve status; returns True when it changed."""
        try:
            s = SleeveStatus(status)
        except ValueError:
            return False
        changed = s is not self.status
        self.status = s
        self.status_reason = reason or self.status_reason
        if changed:
            self.event("status", f"{self.name} -> {status} ({reason or self.status_reason})")
        return changed

    # ------------------------------------------------------- sleeve getters
    def lots_open(self, symbol: str) -> list[dict]:
        return self.ledger.open_lots(self.name, symbol)

    def newest_lot(self, symbol: str) -> Optional[dict]:
        return self.ledger.newest_open_lot(self.name, symbol)

    def signed_position(self, symbol: str) -> Optional[int]:
        lots = self.lots_open(symbol)
        if not lots:
            return None
        return sum(int(l["contracts"]) * (1 if str(l["side"]) == "buy" else -1)
                   for l in lots)

    def coin_exposure(self, symbol: str) -> int:
        """|open lot contracts| for this (sleeve, symbol)."""
        return sum(int(l["contracts"]) for l in self.lots_open(symbol))

    def lots_count(self, symbol: str) -> int:
        return len(self.lots_open(symbol))

    def max_lots(self, symbol: str) -> int:
        return int(self.cfg_get("max_lots", 5))

    def entry_side_for(self, symbol: str) -> Side:
        return self._entry_side.get(symbol, self.entry_side)

    def realized_pnl(self, symbol: str) -> float:
        return float(self.sleeve_pnl.get(symbol, 0.0))

    def note_pnl(self, symbol: str, delta: float) -> None:
        self.sleeve_pnl[symbol] = self.realized_pnl(symbol) + float(delta)
        self.sleeve_hwm[symbol] = max(self.sleeve_hwm.get(symbol, 0.0),
                                      self.sleeve_pnl[symbol])

    def lot_exposure_usd(self) -> float:
        rwa = 0.0
        for s in self.symbols:
            for l in self.lots_open(s):
                rwa += int(l["contracts"]) * float(l["entry_price"])
        return rwa

    def atr(self, symbol: str, resolution: str = "1D") -> Optional[float]:
        from core.indicators import atr as _atr
        df = self.store.frame(symbol, resolution)
        period = int(self.cfg_get("atr_period", 14))
        if df.empty or len(df) < period + 1:
            return None
        val = _atr(df, period)
        if val.empty or pd.isna(val.iloc[-1]):
            return None
        return float(val.iloc[-1])

    # ------------------------------------------------------------- ordering
    def _veto_reason(self, symbol: str, tick: Optional[Ticker],
                     price: float) -> Optional[str]:
        p = self.products.get(symbol)
        if p is None or p.trading_status != "operational":
            return "product_not_operational"
        if tick is None:
            return "no_ticker"
        cap_fn = getattr(self, "spread_cap", None)
        cap = cap_fn(now_utc()) if callable(cap_fn) else self.cfg_get("spread_max_pct", None)
        if cap is not None:
            sp = tick.spread_pct
            if sp is not None and float(sp) > float(cap):
                return f"spread_too_wide ({float(sp):.4f} > {float(cap)})"
        if p.price_band_low and price < float(p.price_band_low):
            return "below_price_band"
        if p.price_band_high and price > float(p.price_band_high):
            return "above_price_band"
        return None

    def record_intent(self, intent: OrderIntent, status: str,
                      requested_price: float | None = None) -> None:
        ok = request_price(intent, requested_price)
        self.ledger.record_order(
            client_order_id=intent.client_order_id, symbol=intent.product_symbol,
            side=str(getattr(intent.side, "value", intent.side)).lower(),
            size=int(intent.size), status=status,
            order_type=str(getattr(intent.order_type, "value", intent.order_type)),
            limit_price=ok, reduce_only=bool(intent.reduce_only),
            product_id=intent.product_id,
            meta={"purpose": intent.purpose, "reason": intent.reason},
        )


# ------------------------------------------------------------------ entries
    async def place_new_lot(
        self,
        symbol: str,
        notional: float,
        price: float,
        reason: str,
        purpose: str = "lot_entry",
    ) -> Optional[str]:
        """Open ONE new lot: write bookkeeping BEFORE sending (spec 3.3), then
        place a post_only limit at `price` (passive entry/add). Returns the
        entry client_order_id, or None when vetoed."""
        if self.halted:
            self.event("entry_skip", "sleeve_halted", symbol)
            return None
        tick = self.marks.get(symbol)
        veto = self._veto_reason(symbol, tick, price)
        if veto:
            self.event("entry_veto", veto, symbol, {"price": price})
            return None
        p = self.products.get(symbol)
        cv = float(getattr(p, "contract_value", 1.0) or 1.0)
        contracts = int(notional / (price * cv)) if (price > 0 and cv > 0) else 0
        if contracts <= 0:
            self.event("entry_veto", "size_floors_to_zero", symbol,
                       {"notional": notional, "price": price})
            return None
        side = self.entry_side_for(symbol)
        oid = new_cid(self.algo, symbol, purpose)
        limit = Decimal(str(round_tick(price, getattr(p, "tick_size", Decimal("0.01")))))
        intent = OrderIntent(
            algo=self.algo, product_symbol=symbol, product_id=getattr(p, "product_id", None),
            side=side, size=contracts, order_type=OrderType.LIMIT,
            limit_price=limit, time_in_force=TimeInForce.GTC, post_only=True,
            reduce_only=False, client_order_id=oid, purpose=purpose,
            reason=reason, meta={"notional": notional},
        )
        self.ledger.record_order(
            client_order_id=oid, symbol=symbol, side=side.value,
            size=contracts, status="new", order_type=OrderType.LIMIT.value,
            limit_price=float(limit), reduce_only=False,
            product_id=getattr(p, "product_id", None),
            meta={"purpose": purpose, "reason": reason},
        )
        self.pending_lots[oid] = {
            "symbol": symbol, "side": side.value, "contracts": contracts,
            "entry_price": float(limit), "notional": notional, "reason": reason,
        }
        try:
            res = await self.exec_port.place(intent)
        except Exception:
            self.pending_lots.pop(oid, None)
            raise
        if not res.accepted:
            self.pending_lots.pop(oid, None)
            self.ledger.record_order(client_order_id=oid, status="rejected")
        return oid if res.accepted else None

    # ------------------------------------------------------------ risk exits
    async def close_lot_market(self, symbol: str, lot: dict, reason: str,
                               purpose: str = "derisk_exit") -> Optional[str]:
        """Whole-lot market reduce-only close (de-risk / software fallback)."""
        side_op = Side.SELL if str(lot["side"]) == "buy" else Side.BUY
        oid = new_cid(self.algo, symbol, purpose)
        intent = OrderIntent(
            algo=self.algo, product_symbol=symbol,
            product_id=getattr(self.products.get(symbol), "product_id", None),
            side=side_op, size=int(lot["contracts"]), order_type=OrderType.MARKET,
            reduce_only=True, client_order_id=oid, purpose=purpose,
            reason=reason, meta={"lot_id": int(lot["id"])},
        )
        self.record_intent(intent, "new")
        self.market_exits[oid] = int(lot["id"])
        try:
            res = await self.exec_port.place(intent)
        except Exception:
            self.market_exits.pop(oid, None)
            raise
        if not res.accepted:
            self.market_exits.pop(oid, None)
            self.ledger.record_order(client_order_id=oid, status="rejected")
            return None
        return oid

    async def close_newest_market(self, symbol: str, reason: str,
                                  purpose: str = "derisk_exit") -> Optional[str]:
        newest = self.newest_lot(symbol)
        if newest is None:
            return None
        return await self.close_lot_market(symbol, newest, reason, purpose)

    # --------------------------------------------------------- TP management
    def _tp_level_for(self, symbol: str, entry: float) -> float:
        side = self.entry_side_for(symbol)
        return lot_tp_price(side.value, entry, float(self.cfg_get("lot_tp_pct", 2.0)))

    async def ensure_resting_tp(self, symbol: str) -> Optional[str]:
        """Exactly ONE resting reduce-only limit TP for the NEWEST open lot
        (spec 2.4); re-placed for the next-newest lot when this one closes."""
        newest = self.newest_lot(symbol)
        if newest is None:
            await self.cancel_tp(symbol)
            return None
        if self.halted or self.status is SleeveStatus.PAUSED:
            return None
        existing = self.resting_tp.get(symbol)
        if existing and self.tp_to_lot.get(existing) == int(newest["id"]):
            return existing                      # correct TP already resting
        if existing:
            await self.cancel_tp(symbol)         # re-place for the new newest
        tp = float(newest.get("tp_price") or 0.0)
        if tp <= 0:
            tp = self._tp_level_for(symbol, float(newest["entry_price"] or 0.0))
        p = self.products.get(symbol)
        side_op = Side.SELL if str(newest["side"]) == "buy" else Side.BUY
        oid = new_cid(self.algo, symbol, "lot_tp")
        intent = OrderIntent(
            algo=self.algo, product_symbol=symbol,
            product_id=getattr(p, "product_id", None),
            side=side_op, size=int(newest["contracts"]), order_type=OrderType.LIMIT,
            limit_price=Decimal(str(round_tick(tp, getattr(p, "tick_size", Decimal("0.01"))))),
            time_in_force=TimeInForce.GTC, reduce_only=True,
            client_order_id=oid, purpose="lot_tp",
            reason=f"lot TP {newest['contracts']}c @ {tp:.8g}",
            meta={"lot_id": int(newest["id"])},
        )
        self.ledger.record_order(
            client_order_id=oid, symbol=symbol, side=side_op.value,
            size=int(newest["contracts"]), status="new",
            order_type=OrderType.LIMIT.value,
            limit_price=float(intent.limit_price), reduce_only=True,
            product_id=getattr(p, "product_id", None),
            meta={"purpose": "lot_tp", "lot_id": int(newest["id"])},
        )
        self.tp_to_lot[oid] = int(newest["id"])
        res = await self.exec_port.place(intent)
        if res.accepted:
            self.resting_tp[symbol] = oid
            return oid
        self.tp_to_lot.pop(oid, None)
        self.ledger.record_order(client_order_id=oid, status="rejected")
        return None

    async def cancel_tp(self, symbol: str) -> None:
        oid = self.resting_tp.pop(symbol, None)
        if not oid:
            return
        try:
            await self.exec_port.cancel(oid, symbol)
        except Exception:
            pass
        self.tp_to_lot.pop(oid, None)
        self.ledger.record_order(client_order_id=oid, status="cancelled")

    async def restore(self) -> None:
        """Restart recovery: re-place the resting TP for each newest lot."""
        for s in self.symbols:
            await self.ensure_resting_tp(s)

    # ---------------------------------------------------------- hedge hooks
    OPTION_CFG_KEYS = (
        "opt_min_dte", "opt_max_dte", "opt_target_delta", "opt_delta_band",
        "opt_max_spread_pct", "opt_min_oi", "opt_reprice_s", "opt_max_reprices",
        "opt_buyback_capture_pct", "opt_roll_delta", "iv_premium",
    )

    def option_cfg(self) -> dict:
        oc: dict[str, Any] = {k: get(self.cfg, k) for k in self.OPTION_CFG_KEYS}
        oc["coverage_ratio"] = self.cfg_get("coverage_ratio", 1.0)
        return oc

    async def setup_options_hedge(self) -> None:
        """Detect live options per underlying; wire an OptionHedgeManager when
        any exist; hedge_mode none + warning otherwise (spec 2.5)."""
        self.unders_with_options: dict[str, bool] = {}
        for s in self.symbols:
            und = s.replace("USD", "")
            try:
                exists = bool(await self.chain.options_exist(und, now_utc()))
            except Exception:
                exists = False
            self.unders_with_options[und] = exists
            if not exists:
                self.event("hedge_mode_none",
                           f"no options for {und}: perp-lot ladder with LIFO only", s)
        if any(self.unders_with_options.values()):
            self.hedge = OptionHedgeManager(
                self.exec_port, self.option_cfg(), self.ledger,
                on_event=self._hedge_event, algo=self.algo,
            )

    def hedge_kind_for(self, symbol: str, call_or_put: str) -> Optional[str]:
        und = symbol.replace("USD", "")
        return call_or_put if self.unders_with_options.get(und) else None

    def _hedge_event(self, kind: str, **kw: Any) -> None:
        feats = {k: str(v) for k, v in kw.items() if k != "symbol"}
        self.event("option_" + kind, kind, kw.get("symbol"), feats)

    async def retarget_hedge(self, symbol: str, now: Optional[datetime] = None,
                             blocked: bool = False) -> None:
        if self.hedge is None:
            return
        kind = None if self.status is SleeveStatus.PAUSED else self._hedge_mode(symbol)
        if kind is None:
            return
        cv = float(getattr(self.products.get(symbol), "contract_value", 1.0) or 1.0)
        expo = self.coin_exposure(symbol) * cv
        blocked = blocked or self.halted or (
            bool(getattr(self.portfolio, "risk_breached", False)) if self.portfolio else False
        )
        await self.hedge.ensure(symbol, kind, float(expo), cv, self.chain,
                                now or now_utc(), blocked=blocked)

    def _hedge_mode(self, symbol: str) -> Optional[str]:
        return None   # subclasses override (ccc/put per sleeve mode)

    async def options_cycle(self, now: datetime) -> None:
        if self.hedge is None:
            return
        for s in self.symbols:
            await self.hedge.cycle(s, self.chain, now)

    # ------------------------------------------------------------- candles
    async def on_candle_close(self, candle: Candle) -> None:
        if candle.symbol in self.symbols and candle.closed:
            self.store.upsert(candle)
        await self.on_candle(candle)

    async def on_candle(self, candle: Candle) -> None:  # subclass hook
        return None

    # -------------------------------------------------------------- ticker
    async def on_ticker(self, symbol: str, ticker: Ticker) -> None:
        if symbol not in self.symbols:
            return
        self.marks[symbol] = ticker
        await self.on_tick(symbol, ticker)
        await self.tp_fallback(symbol)

    async def on_tick(self, symbol: str, ticker: Ticker) -> None:  # subclass hook
        return None

    async def tp_fallback(self, symbol: str) -> None:
        """Software fallback: mark price THROUGH the lot TP (spec 2.4) while
        no resting TP order covers the newest lot (or after restart)."""
        ticker = self.marks.get(symbol)
        newest = self.newest_lot(symbol)
        if ticker is None or newest is None:
            return
        mark = ticker.mark_price or ticker.best_bid or ticker.best_ask
        tp = float(newest.get("tp_price") or 0.0)
        if mark is None or tp <= 0:
            return
        if self.resting_tp.get(symbol) or self.halted or self.status is SleeveStatus.PAUSED:
            return   # resting limit handles it on the exchange side
        m = float(mark)
        side = str(newest["side"])
        if (side == "buy" and m >= tp) or (side == "sell" and m <= tp):
            self.event("tp_fallback", f"mark {m:g} crossed lot TP {tp:g}",
                       symbol, {"lot_id": int(newest["id"])})
            await self.close_newest_market(symbol, "tp_software_fallback",
                                           purpose="tp_fallback")

    # ---------------------------------------------------------------- fills
    async def on_fill(self, fill: FillEvent) -> dict[str, Any]:
        """Common LIFO fill bookkeeping. Returns a summary dict."""
        oid = str(fill.client_order_id)
        out: dict[str, Any] = {"handled": False}
        hf = self._hedge_on_fill(fill, oid)
        if hf:
            out["handled"] = True
            out["hedge"] = hf
            return out
        # 1. entry fill -> ONE lot (recorded before send, confirmed on fill)
        pend = self.pending_lots.pop(oid, None)
        if pend is not None and not fill.reduce_only:
            symbol = pend["symbol"]
            tp = self._tp_level_for(symbol, float(fill.price))
            lot_id = self.ledger.open_lot(
                self.name, symbol, pend["side"], int(fill.size),
                float(fill.price), entry_client_oid=oid, tp_price=tp,
                confirmed=True, meta={"reason": pend["reason"]},
            )
            self.ledger.record_order(client_order_id=oid, avg_fill_price=float(fill.price),
                                     filled_size=int(fill.size), status="filled")
            self.event("lot_open", f"lot {lot_id} opened @ {fill.price}",
                       symbol, {"lot_id": lot_id, "tp_price": tp,
                                "contracts": int(fill.size), "kind": "entry"})
            out["handled"] = True
            out["new_lot"] = lot_id
            await self.after_lot_open(symbol, lot_id)
            return out
        # 2. resting lot-TP / distribution sell fill -> close that lot (LIFO)
        lot_id = self.tp_to_lot.pop(oid, None)
        if lot_id is not None:
            lot = self.ledger.lot(lot_id)
            if lot and lot["status"] == "open":
                self.ledger.close_lot(lot_id, float(fill.price), _iso(fill.ts),
                                      exit_client_oid=oid)
                self.ledger.record_order(client_order_id=oid, avg_fill_price=float(fill.price),
                                         filled_size=int(fill.size), status="filled")
                self.resting_tp.pop(str(lot["symbol"]), None)
                sym = lot["symbol"]
                self.event("lot_close",
                           f"lot {lot_id} closed @ {fill.price} (LIFO TP)",
                           sym, {"lot_id": lot_id, "close": float(fill.price),
                                 "kind": "exit"})
                self.note_pnl(sym, realized_pnl_of(lot, float(fill.price)))
                out["handled"] = True
                out["closed_lot"] = lot_id
                await self.after_lot_closed(sym, lot_id)
                return out
        # 3. market exit fill (de-risk / software TP fallback / flatten)
        mid = self.market_exits.pop(oid, None)
        if mid is not None:
            lot = self.ledger.lot(mid)
            if lot and lot["status"] == "open":
                self.ledger.close_lot(mid, float(fill.price), _iso(fill.ts),
                                      exit_client_oid=oid)
                self.ledger.record_order(client_order_id=oid, avg_fill_price=float(fill.price),
                                         filled_size=int(fill.size), status="filled")
                self.resting_tp.pop(str(lot["symbol"]), None)
                sym = str(lot["symbol"])
                self.event("lot_close", f"lot {mid} closed (market)", sym,
                           {"lot_id": mid, "close": float(fill.price),
                            "reduce_only": bool(fill.reduce_only), "kind": "exit"})
                self.note_pnl(sym, realized_pnl_of(lot, float(fill.price)))
                out["handled"] = True
                out["closed_lot"] = mid
                await self.after_lot_closed(sym, mid)
                return out
        return out

    def _hedge_on_fill(self, fill: FillEvent, oid: str) -> Optional[dict]:
        if self.hedge is None:
            return None
        for und, st in self.hedge.state.items():
            if st.pending_oid != oid:
                continue
            size = int(fill.size)
            fside = getattr(fill.side, "value", fill.side)
            if fside == "sell":
                self.hedge.resolve_pending(st, oid)
                st.contracts += size
                st.avg_entry_premium = (
                    float(fill.price) if st.avg_entry_premium <= 0
                    else (st.avg_entry_premium + float(fill.price)) / 2.0
                )
                if st.hedge_row_id is None:
                    st.hedge_row_id = self.ledger.open_hedge(
                        self.name, str(fill.symbol), fill.product_id, "sell",
                        st.contracts, float(fill.price), entry_client_oid=oid,
                        meta={"underlying": und},
                    )
                return {"action": "open", "contracts": st.contracts}
            if fside == "buy":
                self.hedge.resolve_pending(st, oid)
                st.contracts = max(0, st.contracts - size)
                if st.contracts == 0:
                    if st.hedge_row_id is not None:
                        self.ledger.close_hedge(st.hedge_row_id, float(fill.price))
                    st.option_symbol = None
                    st.avg_entry_premium = 0.0
                    st.kind = None
                    st.hedge_row_id = None
                return {"action": "close", "contracts": st.contracts}
        return None

    # -------------------------------------------------------- lot lifecycle
    async def after_lot_open(self, symbol: str, lot_id: int) -> None:
        """Manage the ladder: ONE TP for the newest lot + hedge re-target."""
        await self.ensure_resting_tp(symbol)
        await self.retarget_hedge(symbol)

    async def after_lot_closed(self, symbol: str, lot_id: int) -> None:
        """Next-newest lot becomes eligible: re-place ITS TP (spec 2.4)."""
        self.resting_tp.pop(symbol, None)
        await self.ensure_resting_tp(symbol)
        await self.retarget_hedge(symbol)

    # ------------------------------------------------------------- de-risk
    async def derisk_step(self, now: datetime | None = None) -> list[int]:
        """One de-risk step (spec 2.6/2.10): close the NEWEST lot newest-first.
        Returns closed lot ids (fills may arrive re-entrantly)."""
        closed: list[int] = []
        for s in self.symbols:
            newest = self.newest_lot(s)
            if newest is None:
                continue
            lot_id = int(newest["id"])
            await self.cancel_tp(s)
            await self.close_lot_market(s, newest, "derisk_step (newest-first exit)")
            lot = self.ledger.lot(lot_id)
            if lot is not None and lot["status"] == "closed":
                closed.append(lot_id)
        return closed

    async def on_timer(self, now: datetime | None = None) -> None:  # subclass hook
        return None

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "sleeve": self.name,
            "status": self.status.value,
            "status_reason": self.status_reason,
            "halted": self.halted,
            "symbols": {},
        }
        for s in self.symbols:
            lots = [{
                "id": int(l["id"]), "side": str(l["side"]),
                "contracts": int(l["contracts"]),
                "entry": float(l["entry_price"] or 0.0), "tp": l["tp_price"],
                "opened_at": str(l["opened_at"]),
            } for l in self.lots_open(s)]
            out["symbols"][s] = {
                "lots": lots,
                "n_lots": len(lots),
                "net_signed": self.signed_position(s),
                "tp_oid": self.resting_tp.get(s),
            }
        return out


def request_price(intent: OrderIntent, fallback: float | None = None) -> float | None:
    """limit_price as float for the orders table."""
    if intent.limit_price is not None:
        return float(intent.limit_price)
    return fallback


