"""RWA Metals & Energy — Positional Trading strategy (prompt A, Part 2).

Glue: bootstrap (universe + warmup + levels), on_candle_close (4H/1D/1H/15m),
on_ticker (liquidity filters + software profit-booking backup + liquidation
monitor), on_fill (ladder fills, bracket re-centre, closes), on_timer
(cooldowns/time stops/rung TTL/portfolio checks) and reconcile (spec 3.3).

The strategy NEVER talks to the exchange directly — only through the
ExecutionPort (spec 3.2). Every order carries a client_order_id built with
core.models.new_client_order_id, every exit is reduce_only=True and every
passive entry is post_only=True. Decision events go through the shared
EventLogger into the `events` table.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from core.events import EventLogger
from core.ledger import LIFOLedger
from core.models import (
    Candle,
    Event,
    FillEvent,
    OrderIntent,
    OrderType,
    ProductInfo,
    Side,
    Ticker,
    TimeInForce,
    new_client_order_id,
    now_utc,
)
from core.ports import ExecutionPort, StrategyPort
from core.state import DeltaSchema

from strategy.rwa_positional.campaign import (
    Campaign,
    LadderPlan,
    campaign_from_meta,
    campaign_to_meta,
    plan_ladder,
    plan_to_campaign,
    warn_martingale,
)
from strategy.rwa_positional.config_defaults import merged_config
from strategy.rwa_positional.levels import (
    Levels,
    Zone,
    atr_from_candles,
    pivots_from_candles,
    swing_points_all,
)
from strategy.rwa_positional.portfolio_limits import PortfolioLimits
from strategy.rwa_positional.profit_booking import (
    effective_leverage_map,
    target_hit,
    time_stop_hit,
    trail_stop_price as trailing_stop_price,
    used_margin_contracts,
)
from strategy.rwa_positional.state_machine import StateMachine
from strategy.rwa_positional.structure import (
    Structure,
    choch_against,
    compute_structure,
    structure_exit,
)
from strategy.rwa_positional.sweep import SweepDetector, filter_sweep_side

logger = logging.getLogger(__name__)

TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "4H": 14400,
              "1D": 86400}
ENERGY_KEYWORDS = (
    "oil", "gas", "energy", "brent", "wti", "crude", "natgas", "natural",
    "heating", "diesel", "gasoline", "ngas", "oandg",
)
MAX_SCALP_LOOKB = 800  # candles per (symbol, tf) ring buffer


@dataclass
class SymbolRuntime:
    symbol: str
    product: ProductInfo
    structure: Structure = field(default_factory=Structure)
    atr1h: float = 0.0
    atr4h: float = 0.0
    zones: list[Zone] = field(default_factory=list)
    prev_day: tuple[float | None, float | None] = (None, None)
    prev_week: tuple[float | None, float | None] = (None, None)
    candles: dict[str, list[Candle]] = field(default_factory=dict)
    cooldown_bars_left: int = 0
    campaign: Campaign | None = None
    campaign_id: int | None = None
    entry_placed_ts: datetime | None = None
    cross_retried: bool = False
    last_ticker: Ticker | None = None
    liquidity_block: str | None = None
    detector: SweepDetector = field(default_factory=SweepDetector)


class RwaPositionalStrategy(StrategyPort):
    name = "rwa_positional"
    algo = "rwa"

    def __init__(
        self,
        cfg: dict[str, Any] | None = None,
        *,
        config: Any | None = None,          # core.config.Config (rwa_positional subtree)
        db_path: str = "state/rwa_positional.db",
        ledger: LIFOLedger | None = None,
        events: EventLogger | None = None,
        now_fn: Any | None = None,
        sleeve: str = "rwa_positional",
    ) -> None:
        user = None
        if cfg is None and config is not None:
            user = getattr(config, "rwa_positional", None)
        elif cfg is not None:
            user = getattr(cfg, "rwa_positional", None) if hasattr(cfg, "rwa_positional") else cfg
        self.cfg = merged_config(user)
        self.db_path = db_path
        self.sleeve = sleeve
        self.now_fn = now_fn or now_utc
        self._ledger = ledger
        self._events = events
        self._db: DeltaSchema | None = None
        self.exec: ExecutionPort | None = None
        self.sm = StateMachine(algo=self.name, prefix="rwa_")
        self.portfolio = PortfolioLimits(self.cfg)
        self.levels_builder = Levels(self.cfg)
        self.runtime: dict[str, SymbolRuntime] = {}
        self.symbols: list[str] = []
        self._martingale_warned = False
        self._halted = False
        self._lev_map = effective_leverage_map(self.cfg)

    # ------------------------------------------------------------- properties
    @property
    def ledger(self) -> LIFOLedger:
        assert self._ledger is not None, "strategy not bootstrapped"
        return self._ledger

    @property
    def events(self) -> EventLogger:
        assert self._events is not None, "strategy not bootstrapped"
        return self._events

    # --------------------------------------------------------------- bootstrap
    async def bootstrap(self, exec_port: ExecutionPort) -> None:
        self.exec = exec_port
        if self._db is None:
            self._db = DeltaSchema(self.db_path)
        if self._events is None:
            self._events = EventLogger(self._db, algo=self.name)
        if self._ledger is None:
            self._ledger = LIFOLedger(self.db_path, prefix="rwa_")
        self.sm = StateMachine(db=self._db, events=self._events,
                               algo=self.name, prefix="rwa_")
        if not self._martingale_warned:
            warn_martingale(self.cfg)
            self._martingale_warned = True

        await self._resolve_universe()
        await self._warmup_candles()
        for sym in self.symbols:
            rt = self.runtime[sym]
            self._recompute_symbol(rt)
            if rt.zones and self.sm.state_of(sym) == "IDLE":
                self.sm.transition(sym, "ARMED", self._feat_snapshot(rt))
        self._reload_open_campaigns()
        mismatches = await self.reconcile()
        self.events.log(
            "reconcile", "bootstrap reconcile",
            features={"mismatches": mismatches, "symbols": self.symbols})

    # ------------------------------------------------------------- universe
    async def _resolve_universe(self) -> None:
        assert self.exec is not None
        products = {p.symbol: p for p in await self.exec.products()}
        chosen: list[tuple[str, ProductInfo]] = []
        for wanted in self.cfg.get("symbols", []):
            p = products.get(wanted)
            if p is None or p.trading_status != "operational" or not p.is_perp:
                self.events.log("skip",
                                f"symbol {wanted} missing / not a live perp — skipped")
                continue
            chosen.append((wanted, p))
        if self.cfg.get("energy_scan", True):
            added = 0
            for sym, p in sorted(products.items()):
                if sym in {s for s, _ in chosen} or not p.is_perp:
                    continue
                hay = " ".join(
                    [p.description] + list(p.tags)
                    + [str(p.raw.get("product_specs", {}).get("tags", ""))]
                    + [str(p.raw.get("short_description", ""))]
                ).lower()
                if any(k in hay for k in ENERGY_KEYWORDS):
                    chosen.append((sym, p))
                    added += 1
            self.events.log(
                "info", "energy scan",
                features={"added": added,
                          "total_symbols": len(chosen),
                          "energy_live": added > 0})
        self.symbols = [s for s, _ in chosen]
        self.runtime = {s: SymbolRuntime(symbol=s, product=p) for s, p in chosen}

    async def _warmup_candles(self) -> None:
        assert self.exec is not None
        end = int(self.now_fn().timestamp())
        for sym, rt in self.runtime.items():
            for tf, bars in (self.cfg.get("warmup_bars") or {}).items():
                secs = TF_SECONDS.get(tf)
                if secs is None:
                    continue
                start = end - int(bars) * secs
                candles = await self.exec.candles(sym, tf, start, end)
                rt.candles[tf] = [c for c in candles if c.closed][-MAX_SCALP_LOOKB:]

    # --------------------------------------------------- per-symbol recompute
    def _recompute_symbol(self, rt: SymbolRuntime) -> None:
        c = self.cfg
        atr_period = int(c.get("atr_period", 14))
        rt.atr1h = atr_from_candles(rt.candles.get(c["zone_tf"], []), atr_period) or 0.0
        rt.atr4h = atr_from_candles(rt.candles.get(c["htf"], []), atr_period) or 0.0
        self._refresh_prev_ranges(rt)
        left4 = int(c.get("pivot_left_4h", 2)); right4 = int(c.get("pivot_right_4h", 2))
        rt.structure = compute_structure(
            rt.candles.get(c["htf"], []), left=left4, right=right4,
            use_ema_tiebreak=bool(c.get("use_ema_tiebreak", True)))
        self._refresh_levels(rt)

    def _refresh_levels(self, rt: SymbolRuntime) -> None:
        c = self.cfg
        candles_1h = rt.candles.get(c["zone_tf"], [])
        sw4 = swing_points_all(
            rt.candles.get(c["htf"], []),
            int(c.get("pivot_left_4h", 2)), int(c.get("pivot_right_4h", 2)))
        rt.zones = self.levels_builder.build(
            candles_1h=candles_1h,
            atr_1h_value=rt.atr1h,
            swings_4h=sw4,
            prev_day_high=rt.prev_day[0], prev_day_low=rt.prev_day[1],
            prev_week_high=rt.prev_week[0], prev_week_low=rt.prev_week[1],
        )

    def _refresh_prev_ranges(self, rt: SymbolRuntime) -> None:
        d = rt.candles.get(self.cfg["levels_tf"], [])
        if len(d) >= 2:
            rd = d[-2]                    # last COMPLETED day
            rt.prev_day = (float(rd.high), float(rd.low))
        if len(d) >= 8:
            wk = d[-9:-2]
            rt.prev_week = (max(float(x.high) for x in wk),
                            min(float(x.low) for x in wk))

    def _feat_snapshot(self, rt: SymbolRuntime) -> dict[str, Any]:
        return {
            "atr1h": rt.atr1h, "atr4h": rt.atr4h,
            "structure": rt.structure.features_dict(),
            "zones": [z.features() for z in rt.zones[:5]],
            "cooldown_bars_left": rt.cooldown_bars_left,
        }

    # ------------------------------------------------- state reload from DB
    def _reload_open_campaigns(self) -> None:
        for cm in self.ledger.get_campaigns():
            if (cm.get("state") or "").upper() == "CLOSED":
                continue
            rt = self.runtime.get(cm["symbol"])
            if rt is None:
                continue
            meta = cm.get("meta") or {}
            if not meta:
                continue
            cam = campaign_from_meta(int(cm["id"]), cm["symbol"], meta)
            rt.campaign = cam
            rt.campaign_id = int(cm["id"])
            st = self.sm.state_of(cm["symbol"])
            if st not in ("BUILDING", "MANAGING", "TRIGGERED", "EXITING"):
                # rebuilt campaign implies we are past TRIGGERED
                if any(r["status"] == "filled" for r in meta.get("rungs", [])):
                    self.sm.transition(cm["symbol"], "BUILDING" if not cam.all_rungs_filled else "MANAGING")
        # TODO(VERIFY): resting rung orders after restart are NOT re-placed here;
        # reconciliation of open orders (exec.open_orders) vs meta rungs TODO.

    # ---------------------------------------------------------------- candles
    async def on_candle_close(self, candle: Candle) -> None:
        rt = self.runtime.get(candle.symbol)
        if rt is None:
            return
        tf = candle.resolution
        buf = rt.candles.setdefault(tf, [])
        if not (buf and buf[-1].ts == candle.ts):
            buf.append(candle)
            if len(buf) > MAX_SCALP_LOOKB:
                del buf[: len(buf) - MAX_SCALP_LOOKB]

        if tf == self.cfg["zone_tf"]:      # 1H: ATR + zones + cooldown countdown
            rt.atr1h = atr_from_candles(buf, int(self.cfg["atr_period"])) or rt.atr1h
            self._refresh_levels(rt)
            if rt.cooldown_bars_left > 0:
                rt.cooldown_bars_left -= 1
                if rt.cooldown_bars_left == 0 and self.sm.state_of(candle.symbol) == "COOLDOWN":
                    self.sm.transition(candle.symbol, "IDLE",
                                       features={"cooldown_done": candle.ts.isoformat()})
                    # Re-arm via bootstrap / next valid setup — not automatically,
                    # so a fresh IDLE is observable (cooldown verification, tests).
        elif tf == self.cfg["trigger_tf"]:  # 15m: sweep trigger
            await self._eval_trigger(rt, candle)
        elif tf == self.cfg["htf"]:        # 4H: structure + structure exit
            rt.structure = compute_structure(
                buf, left=int(self.cfg["pivot_left_4h"]),
                right=int(self.cfg["pivot_right_4h"]),
                use_ema_tiebreak=bool(self.cfg.get("use_ema_tiebreak", True)))
            rt.atr4h = atr_from_candles(buf, int(self.cfg["atr_period"])) or rt.atr4h
            cam = rt.campaign
            if cam is not None and structure_exit(
                    buf, cam, cam.direction, left=int(self.cfg["pivot_left_4h"]),
                    right=int(self.cfg["pivot_right_4h"])):
                await self._request_exit(rt, "structure_exit")
        elif tf == self.cfg["levels_tf"]:  # 1D: prev day/week + zone rebuild
            self._refresh_prev_ranges(rt)
            self._refresh_levels(rt)

    # ----------------------------------------------------------- sweep trigger
    async def _eval_trigger(self, rt: SymbolRuntime, candle: Candle) -> None:
        sym = rt.symbol
        state = self.sm.state_of(sym)
        if rt.campaign is not None or state not in ("IDLE", "ARMED"):
            return
        if rt.cooldown_bars_left > 0 or self._halted or self.portfolio.state.halted:
            return
        if not rt.zones or rt.atr1h <= 0:
            return
        c = self.cfg
        for zone in rt.zones:
            sig = rt.detector.feed(
                sym, rt.candles.get(self.cfg["trigger_tf"], []), zone, rt.atr1h,
                sweep_min_atr=float(c["sweep_min_atr"]),
                reclaim_bars=int(c["reclaim_bars"]),
                volume_mult=float(c["sweep_volume_mult"]),
                wick_frac=float(c["sweep_wick_frac"]),
                bias=rt.structure.bias,
                range_mid=rt.structure.range_mid,
                allow_countertrend=bool(c.get("allow_countertrend", False)),
                volume_sma_len=int(c.get("volume_sma_len", 20)),
            )
            if sig is None:
                continue
            ticker = await self.exec.ticker(sym) if self.exec else None
            rt.last_ticker = ticker or rt.last_ticker
            liq = self._liquidity_check(rt, rt.last_ticker)
            if not liq[0]:
                self.events.log("risk_veto", f"liquidity filter blocks {sig.kind}",
                                symbol=sym, features={"reason": liq[1],
                                                      "signal": sig.features_dict()})
                continue
            wanted = "long" if sig.kind == "sell_side" else "short"
            direction = filter_sweep_side(
                wanted_direction=wanted,
                bias=rt.structure.bias,
                zone_price=zone.price,
                range_mid=rt.structure.range_mid,
                zone_score=zone.score,
                allow_countertrend=bool(c.get("allow_countertrend", False)),
                counter_min_score=3,
            )
            if direction is None:
                self.events.log("skip", "direction filter blocks sweep",
                                symbol=sym, features={
                                    "signal": sig.features_dict(),
                                    "bias": rt.structure.bias,
                                    "range_mid": rt.structure.range_mid})
                continue
            self.events.log("signal", f"{sig.kind} sweep confirmed -> {direction}",
                            symbol=sym, features={"signal": sig.features_dict(),
                                                  "structure": rt.structure.features_dict()})
            await self._enter_campaign(rt, zone, sig, direction, rt.last_ticker)
            return

    # ------------------------------------------------------------- liquidity
    def _liquidity_check(self, rt: SymbolRuntime, ticker: Ticker | None) -> tuple[bool, str]:
        if ticker is None:
            return False, "no ticker"
        c = self.cfg
        spread = ticker.spread_pct
        max_spread = Decimal(str(c["max_spread_pct"]))
        if spread is None or spread > max_spread:
            return False, f"spread {spread} > {max_spread}"
        min_turn = c.get("min_turnover_usd")
        if min_turn is not None and (ticker.turnover_usd is None
                                     or ticker.turnover_usd < Decimal(str(min_turn))):
            return False, f"turnover {ticker.turnover_usd} < {min_turn}"
        p = rt.product
        if p.trading_status != "operational" or p.only_reduce_only_orders_allowed:
            return False, f"trading_status={p.trading_status}"
        mark = ticker.mark_price
        band_buf = float(c.get("price_band_buffer_pct", 1.0)) / 100.0
        if mark is not None:
            if p.price_band_high is not None and mark > p.price_band_high * (Decimal("1") - Decimal(str(band_buf))):
                return False, "price near price_band high"
            if p.price_band_low is not None and mark < p.price_band_low * (Decimal("1") + Decimal(str(band_buf))):
                return False, "price near price_band low"
        return True, "ok"

    # ------------------------------------------------------------ entry flow
    def _leverage_for(self, symbol: str) -> float:
        lev = self.cfg.get("leverage")
        if isinstance(lev, dict):
            lev = lev.get(symbol, lev.get("default"))
        if lev is None:
            lev = self.cfg.get("default_leverage_fallback", 5.0)
        return min(float(lev or 0.0) or float(self.cfg.get("default_leverage_fallback", 5.0)),
                   float(self.cfg.get("max_leverage", 10)))

    async def _enter_campaign(
        self, rt: SymbolRuntime, zone: Zone, sig: Any, direction: str,
        ticker: Ticker,
    ) -> None:
        sym = rt.symbol
        account = await self.exec.account()
        equity = float(account.equity or 0.0)
        open_camps = [c for c in self.ledger.get_campaigns()
                      if (c.get("state") or "").upper() not in ("CLOSED",)]
        total_margin_used = self._sum_open_margin(open_camps)
        ok, why = self.portfolio.can_open_campaign(
            open_campaigns=open_camps, equity=equity,
            total_margin_used=total_margin_used, symbol=sym)
        if not ok:
            self.events.log("risk_veto", "portfolio blocks new campaign",
                            symbol=sym, features={"reason": why})
            return

        leverage = self._leverage_for(sym)
        product = rt.product
        best_bid = float(ticker.best_bid) if ticker.best_bid is not None else None
        best_ask = float(ticker.best_ask) if ticker.best_ask is not None else None
        if direction == "long":
            e1 = best_bid
        else:
            e1 = best_ask
        if not e1 or e1 <= 0:
            self.events.log("skip", "no best bid/ask for entry", symbol=sym)
            return
        mult = float(self.cfg.get("invalidation_atr_mult", 0.5))
        invalidation = (
            sig.wick_extreme - mult * rt.atr1h if direction == "long"
            else sig.wick_extreme + mult * rt.atr1h
        )
        plan = plan_ladder(
            direction=direction,
            e1_price=e1,
            atr_1h_value=rt.atr1h,
            invalidation_price=invalidation,
            equity=equity,
            leverage=leverage,
            weights=list(self.cfg.get("rung_weights") or []),
            rungs=int(self.cfg.get("rungs", 4)),
            ladder_atr_mult=list(self.cfg.get("ladder_atr_mult") or []),
            ladder_mode=str(self.cfg.get("ladder_mode", "against")),
            campaign_margin_pct=float(self.cfg.get("campaign_margin_pct", 15.0)),
            campaign_risk_pct=float(self.cfg.get("campaign_risk_pct", 1.0)),
            contract_value=float(product.contract_value),
            tick=float(product.tick_size),
            target_pct_of_margin=float(self.cfg.get("profit_target_pct_of_margin", 20.0)),
        )
        if plan.skipped_reason or not plan.rungs:
            self.events.log("skip", "ladder planner skipped signal",
                            symbol=sym, features={"skipped_reason": plan.skipped_reason})
            return

        # -------- PERSIST FIRST (write-before-send, spec 3.3) --------
        feat = {"zone": zone.features(), "signal": sig.features_dict(),
                "structure": rt.structure.features_dict(),
                "e1": e1, "invalidation": invalidation, "equity": equity,
                "leverage": leverage, "plan_rungs":
                    [{"index": r.index, "price": r.price, "contracts": r.contracts,
                      "margin": r.margin} for r in plan.rungs]}
        if self.sm.state_of(sym) == "IDLE":
            self.sm.transition(sym, "ARMED",
                               {"note": "armed at entry", "zone": zone.features()})
        self.sm.transition(sym, "TRIGGERED", feat)
        campaign_id = self.ledger.create_campaign(
            symbol=sym, direction=direction, state="TRIGGERED",
            zone=zone.features(), meta={"bootstrap": feat})
        cam = plan_to_campaign(
            plan, campaign_id=campaign_id, symbol=sym,
            zone_price=zone.price, zone_score=zone.score,
            zone_upper=zone.upper, zone_lower=zone.lower,
            contract_value=float(product.contract_value),
            tick=float(product.tick_size),
            target_pct_of_margin=float(self.cfg.get("profit_target_pct_of_margin", 20.0)),
            created_ts=self.now_fn(),
            features={"signal": sig.features_dict(),
                      "structure": rt.structure.features_dict()})
        rt.campaign = cam
        rt.campaign_id = campaign_id
        self.ledger.update_campaign(campaign_id, state="TRIGGERED",
                                    meta=campaign_to_meta(cam))
        self.ledger.update_campaign(campaign_id, entry_avg=e1,
                                    tp_price=cam.tp_price,
                                    contracts=0)
        # -------- THEN send rung 1 --------
        await self._place_rung1(rt, plan, ticker)

    def _sum_open_margin(self, open_camps: list[dict[str, Any]]) -> float:
        """Approx total used margin of open campaigns (notional / leverage).
        Uses entry_avg from the campaign row when recorded."""
        total = 0.0
        for cm in open_camps:
            meta = cm.get("meta") or {}
            rungs = meta.get("rungs") or []
            wraps = [float(r.get("contracts", 0) or 0) for r in rungs if r.get("status") == "filled"]
            fills = meta.get("fills") or []
            lev = float(meta.get("leverage") or 1.0) or 1.0
            notional = sum(float(f.get("size", 0)) * float(f.get("price", 0)) for f in fills)
            total += notional / lev if lev > 0 else 0.0
            _ = wraps
        return total

    async def _place_rung1(self, rt: SymbolRuntime, plan: LadderPlan, ticker: Ticker) -> None:
        sym = rt.symbol
        cam = rt.campaign
        assert cam is not None
        rung1 = cam.rungs[0]
        side = Side.BUY if cam.direction == "long" else Side.SELL
        intent = OrderIntent(
            algo=self.algo, product_symbol=sym,
            product_id=rt.product.product_id,
            side=side, size=rung1.contracts,
            order_type=OrderType.LIMIT,
            limit_price=Decimal(str(rung1.price)),
            time_in_force=TimeInForce.GTC, post_only=True,
            purpose="rung1",
            reason=(f"rung1 {cam.direction} @ {rung1.price} zone {cam.zone_price} "
                    f"score {cam.zone_score}"),
            meta={"rung": 1, "campaign_id": cam.id},
        )
        intent.with_client_id(self.algo, sym, "rung1")
        rung1.client_order_id = intent.client_order_id
        rung1.status = "placed"
        rt.entry_placed_ts = self.now_fn()
        self.ledger.record_order(
            client_order_id=intent.client_order_id, symbol=sym,
            side=side.value, size=rung1.contracts, order_type="limit_order",
            limit_price=rung1.price, reduce_only=False,
            product_id=rt.product.product_id, status="new",
            meta={"purpose": "rung1", "campaign_id": cam.id, "rung": 1})
        res = await self.exec.place(intent)
        self.ledger.record_order(
            client_order_id=intent.client_order_id, symbol=sym,
            side=side.value, size=rung1.contracts,
            filled_size=int(res.filled_size or 0),
            avg_fill_price=float(res.avg_fill_price) if res.avg_fill_price else None,
            status="open" if res.accepted else "rejected",
            order_type="limit_order",
            limit_price=rung1.price,
            product_id=rt.product.product_id,
            meta={"purpose": "rung1", "campaign_id": cam.id, "rung": 1})
        self.ledger.update_campaign(cam.id, meta=campaign_to_meta(cam))
        if res.accepted:
            self.events.log("entry", f"rung1 placed {sym} {side.value} {rung1.contracts} @ {rung1.price}",
                            symbol=sym, features={"intent": _intent_dump(intent),
                                                  "res_status": str(res.status)})
        else:
            self.events.log("risk_veto", f"rung1 rejected: {res.error_code}",
                            symbol=sym, features={"error": res.error_message})
            await self._abort_entry(rt, f"entry_rejected:{res.error_code}")

    async def _abort_entry(self, rt: SymbolRuntime, reason: str) -> None:
        sym = rt.symbol
        cam = rt.campaign
        if cam is not None and cam.id is not None and not cam.fills:
            self.ledger.close_campaign(cam.id, close_reason=reason)
        rt.campaign = None
        rt.campaign_id = None
        if self.sm.state_of(sym) == "TRIGGERED":
            self.sm.transition(sym, "IDLE", features={"reason": reason})

    # ----------------------------------------------------------- entry timeout
    async def _entry_timeout_check(self, rt: SymbolRuntime, now: datetime) -> None:
        sym = rt.symbol
        cam = rt.campaign
        if cam is None or rt.entry_placed_ts is None:
            return
        if self.sm.state_of(sym) != "TRIGGERED":
            return
        rung1 = cam.rungs[0] if cam.rungs else None
        if rung1 is None or rung1.status == "filled":
            return
        timeout = float(self.cfg.get("entry_timeout_s", 20))
        if (now - rt.entry_placed_ts).total_seconds() < timeout:
            return
        if not rt.cross_retried:
            rt.cross_retried = True
            ticker = await self.exec.ticker(sym)
            rt.last_ticker = ticker or rt.last_ticker
            t = rt.last_ticker
            tick = float(rt.product.tick_size)
            side = Side.BUY if cam.direction == "long" else Side.SELL
            if t is None or (t.best_ask if cam.direction == "long" else t.best_bid) is None:
                await self._fallback_market_or_abort(rt, cam)
                return
            cross = (float(t.best_ask) if cam.direction == "long" else float(t.best_bid))
            cross += (2 * tick) if cam.direction == "long" else -(2 * tick)
            await self._cancel(rung1.client_order_id, sym)
            intent = OrderIntent(
                algo=self.algo, product_symbol=sym, product_id=rt.product.product_id,
                side=side, size=rung1.contracts, order_type=OrderType.LIMIT,
                limit_price=Decimal(str(round(cross / tick) * tick)),
                time_in_force=TimeInForce.IOC, post_only=False,
                purpose="rung1_ioc", reason="entry timeout -> IOC cross fallback",
                meta={"rung": 1, "campaign_id": cam.id})
            intent.with_client_id(self.algo, sym, "rung1_ioc")
            rung1.client_order_id = intent.client_order_id
            self.ledger.record_order(
                client_order_id=intent.client_order_id, symbol=sym,
                side=side.value, size=rung1.contracts, order_type="limit_order",
                limit_price=float(intent.limit_price), reduce_only=False,
                product_id=rt.product.product_id, status="new",
                meta={"purpose": "rung1_ioc", "campaign_id": cam.id, "rung": 1})
            res = await self.exec.place(intent)
            self.ledger.record_order(
                client_order_id=intent.client_order_id, symbol=sym,
                side=side.value, size=rung1.contracts,
                filled_size=int(res.filled_size or 0),
                avg_fill_price=float(res.avg_fill_price) if res.avg_fill_price else None,
                status="open" if res.accepted else "rejected", order_type="limit_order",
                limit_price=float(intent.limit_price),
                product_id=rt.product.product_id,
                meta={"purpose": "rung1_ioc", "campaign_id": cam.id, "rung": 1})
            rt.entry_placed_ts = self.now_fn()
            self.ledger.update_campaign(cam.id, meta=campaign_to_meta(cam))
            self.events.log("info", "entry IOC cross fallback placed",
                            symbol=sym, features={"price": float(intent.limit_price)})
            if not res.accepted:
                await self._abort_entry(rt, "ioc_fallback_rejected")
        else:
            if bool(self.cfg.get("allow_market", False)):
                await self._fallback_market_or_abort(rt, cam)
            else:
                await self._abort_entry(rt, "entry_timeout_unfilled")

    async def _fallback_market_or_abort(self, rt: SymbolRuntime, cam: Campaign) -> None:
        # never a pure market order unless allow_market (spec 2.7)
        sym = rt.symbol
        if not bool(self.cfg.get("allow_market", False)):
            await self._abort_entry(rt, "entry_timeout_unfilled_no_market")
            return
        rung1 = cam.rungs[0]
        side = Side.BUY if cam.direction == "long" else Side.SELL
        await self._cancel(rung1.client_order_id, sym)
        intent = OrderIntent(
            algo=self.algo, product_symbol=sym, product_id=rt.product.product_id,
            side=side, size=rung1.contracts, order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC, post_only=False, reduce_only=False,
            purpose="rung1_market", reason="entry timeout -> market (allow_market)",
            meta={"rung": 1, "campaign_id": cam.id})
        intent.with_client_id(self.algo, sym, "rung1_market")
        res = await self.exec.place(intent)
        self.ledger.record_order(
            client_order_id=intent.client_order_id, symbol=sym,
            side=side.value, size=rung1.contracts, order_type="market_order",
            reduce_only=False, product_id=rt.product.product_id,
            filled_size=int(res.filled_size or 0),
            avg_fill_price=float(res.avg_fill_price) if res.avg_fill_price else None,
            status="open" if res.accepted else "rejected",
            meta={"purpose": "rung1_market", "campaign_id": cam.id, "rung": 1})
        rt.entry_placed_ts = self.now_fn()

    # ------------------------------------------------------------ fills
    async def on_fill(self, fill: FillEvent) -> None:
        sym = fill.symbol
        rt = self.runtime.get(sym)
        if rt is None:
            return
        order = None
        try:
            order = self.ledger.get_order(fill.client_order_id)
        except Exception:
            order = None
        meta = (order or {}).get("meta") or {}
        self.events.log("fill", f"fill {sym} {fill.side.value} {fill.size} @ {fill.price}",
                        symbol=sym, features={"client_order_id": fill.client_order_id,
                                              "reduce_only": fill.reduce_only,
                                              "order_meta": meta})
        if str(meta.get("purpose", "")).startswith("bracket"):
            return  # bracket placement ack, not a fill of a campaign rung
        if fill.reduce_only:
            cid = meta.get("campaign_id") or rt.campaign_id
            if cid is None:
                # exchange-side TP/SL with an oid we did not record
                open_camps = [cm for cm in self.ledger.get_campaigns(symbol=sym)
                              if (cm.get("state") or "").upper() != "CLOSED"]
                cid = int(open_camps[0]["id"]) if open_camps else None
            await self._handle_exit_fill(rt, int(cid), fill,
                                         reason=str(meta.get("purpose", "exchange_tp")))
            return
        # entry fill
        cam = rt.campaign
        if cam is None:
            return
        rung_index = int(meta.get("rung", 1))
        await self._handle_entry_fill(rt, cam, rung_index, fill)

    async def _handle_entry_fill(self, rt: SymbolRuntime, cam: Campaign,
                                 rung_index: int, fill: FillEvent) -> None:
        sym = rt.symbol
        price = float(fill.price)
        size = int(fill.size)
        # idempotency: same rung + oid already booked?
        if any(f.get("client_order_id") == fill.client_order_id for f in cam.fills):
            return
        lot_id = self.ledger.open_lot(
            self.sleeve, sym, fill.side.value, size, price,
            entry_client_oid=fill.client_order_id, tp_price=cam.tp_price,
            confirmed=True, meta={"campaign_id": cam.id, "rung": rung_index})
        cam.fills.append({"rung": rung_index, "size": size, "price": price,
                          "ts": fill.ts.isoformat(), "lot_id": lot_id,
                          "client_order_id": fill.client_order_id})
        rung = cam.rung_by_index(rung_index)
        if rung is not None:
            rung.status = "filled"
            rung.client_order_id = fill.client_order_id
        cam.recentre_tp_sl()
        self.events.log("fill", f"entry fill rung {rung_index}: {size} @ {price}",
                        symbol=sym, features={"avg_entry": cam.avg_entry,
                                              "contracts": cam.contracts,
                                              "margin_used": cam.margin_used})
        if self.sm.state_of(sym) == "TRIGGERED":
            self.sm.transition(sym, "BUILDING",
                               features={"avg_entry": cam.avg_entry,
                                         "contracts": cam.contracts})
        self.ledger.update_campaign(
            cam.id, state="BUILDING", entry_avg=cam.avg_entry,
            contracts=cam.contracts, tp_price=cam.tp_price,
            meta=campaign_to_meta(cam))
        if cam.all_rungs_filled:
            self.sm.transition(sym, "MANAGING",
                               features={"avg_entry": cam.avg_entry,
                                         "contracts": cam.contracts})
            self.ledger.update_campaign(cam.id, state="MANAGING",
                                        meta=campaign_to_meta(cam))
        # place any remaining resting rungs (post r1 fill; spec 2.7)
        await self._place_resting_rungs(rt)
        # re-centre TP/SL via bracket after every fill (spec 2.9)
        await self._send_bracket(rt, reason="recentre_after_fill")

    async def _place_resting_rungs(self, rt: SymbolRuntime) -> None:
        cam = rt.campaign
        assert cam is not None
        sym = rt.symbol
        for rung in cam.rungs:
            if rung.index == 1 or rung.status != "planned":
                continue
            side = Side.BUY if cam.direction == "long" else Side.SELL
            intent = OrderIntent(
                algo=self.algo, product_symbol=sym,
                product_id=rt.product.product_id,
                side=side, size=rung.contracts, order_type=OrderType.LIMIT,
                limit_price=Decimal(str(rung.price)),
                time_in_force=TimeInForce.GTC, post_only=True,
                purpose=f"rung_add", reason=f"ladder rung {rung.index} resting post-only",
                meta={"rung": rung.index, "campaign_id": cam.id})
            intent.with_client_id(self.algo, sym, f"rung{rung.index}")
            rung.client_order_id = intent.client_order_id
            rung.status = "placed"
            self.ledger.record_order(
                client_order_id=intent.client_order_id, symbol=sym,
                side=side.value, size=rung.contracts, order_type="limit_order",
                limit_price=rung.price, reduce_only=False,
                product_id=rt.product.product_id, status="new",
                meta={"purpose": "rung_add", "campaign_id": cam.id,
                      "rung": rung.index})
            res = await self.exec.place(intent)
            self.ledger.record_order(
                client_order_id=intent.client_order_id, symbol=sym,
                side=side.value, size=rung.contracts, order_type="limit_order",
                limit_price=rung.price, reduce_only=False,
                product_id=rt.product.product_id,
                filled_size=int(res.filled_size or 0),
                avg_fill_price=float(res.avg_fill_price) if res.avg_fill_price else None,
                status="open" if res.accepted else "rejected",
                meta={"purpose": "rung_add", "campaign_id": cam.id,
                      "rung": rung.index})
            if not res.accepted:
                rung.status = "planned"  # retry on next timer tick
                self.events.log("risk_veto", f"rung {rung.index} place failed: {res.error_code}",
                                symbol=sym)
        self.ledger.update_campaign(cam.id, meta=campaign_to_meta(cam))

    async def _send_bracket(self, rt: SymbolRuntime, reason: str) -> None:
        """PUT /orders/bracket: re-centre TP/SL around avg entry (spec 2.9)."""
        cam = rt.campaign
        assert cam is not None
        sym = rt.symbol
        if not cam.fills:
            return
        sl, tp = cam.sl_price, cam.tp_price
        sgn = 1.0 if cam.direction == "long" else -1.0
        # protect: SL must sit on the safe side of avg entry
        if (cam.direction == "long" and sl >= cam.avg_entry) or (
                cam.direction == "short" and sl <= cam.avg_entry):
            self.events.log("risk_veto", "bracket SL invalid vs avg entry",
                            symbol=sym, features={"sl": sl, "avg": cam.avg_entry})
            return
        intent = OrderIntent(
            algo=self.algo, product_symbol=sym, product_id=rt.product.product_id,
            side=Side.SELL if cam.direction == "long" else Side.BUY,
            size=cam.contracts, order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC, reduce_only=True,
            bracket_stop_loss_price=Decimal(str(sl)),  # TODO(VERIFY): Delta PUT /orders/bracket field name (bracket_stop_loss_price vs bracket_stop_loss_limit_price)
            bracket_take_profit_price=Decimal(str(tp)),
            stop_trigger_method=str(self.cfg.get("stop_trigger_method", "mark_price")),
            purpose="bracket", reason=reason, meta={"campaign_id": cam.id})
        intent.with_client_id(self.algo, sym, "bracket")
        self.ledger.record_order(
            client_order_id=intent.client_order_id, symbol=sym,
            side=(Side.SELL if cam.direction == "long" else Side.BUY).value,
            size=cam.contracts, order_type="bracket",
            reduce_only=True, product_id=rt.product.product_id, status="new",
            meta={"purpose": "bracket", "campaign_id": cam.id})
        res = await self.exec.place(intent)
        self.ledger.record_order(
            client_order_id=intent.client_order_id, symbol=sym,
            side=(Side.SELL if cam.direction == "long" else Side.BUY).value,
            size=cam.contracts, order_type="bracket", reduce_only=True,
            product_id=rt.product.product_id,
            filled_size=int(res.filled_size or 0),
            avg_fill_price=float(res.avg_fill_price) if res.avg_fill_price else None,
            status="open" if res.accepted else "rejected",
            meta={"purpose": "bracket", "campaign_id": cam.id,
                  "sl": sl, "tp": tp})
        self.events.log("info", f"bracket re-centred ({reason})",
                        symbol=sym, features={"sl": sl, "tp": tp,
                                              "avg_entry": cam.avg_entry,
                                              "accepted": res.accepted})

    # ------------------------------------------------------------- exits
    async def _request_exit(self, rt: SymbolRuntime, reason: str) -> None:
        sym = rt.symbol
        cam = rt.campaign
        if cam is None:
            return
        state = self.sm.state_of(sym)
        if state in ("EXITING", "COOLDOWN", "HALTED"):
            return
        self.sm.transition(sym, "EXITING", features={"reason": reason})
        self.ledger.update_campaign(cam.id, state="EXITING",
                                    meta=campaign_to_meta(cam))
        await self._cancel_unfilled_rungs(rt)
        # close the whole campaign with an aggressive reduce-only order
        ticker = await self.exec.ticker(sym)
        rt.last_ticker = ticker or rt.last_ticker
        t = rt.last_ticker
        tick = float(rt.product.tick_size)
        side = Side.SELL if cam.direction == "long" else Side.BUY
        if cam.direction == "long":
            px = float(t.best_bid) if t and t.best_bid else cam.avg_entry
            px -= 2 * tick  # cross
        else:
            px = float(t.best_ask) if t and t.best_ask else cam.avg_entry
            px += 2 * tick
        order_type = (OrderType.MARKET if bool(self.cfg.get("allow_market", False))
                      else OrderType.LIMIT)
        intent = OrderIntent(
            algo=self.algo, product_symbol=sym, product_id=rt.product.product_id,
            side=side, size=cam.contracts, order_type=order_type,
            limit_price=None if order_type == OrderType.MARKET else
            Decimal(str(round(px / tick) * tick)),
            time_in_force=TimeInForce.IOC, post_only=False, reduce_only=True,
            purpose="exit", reason=reason, meta={"campaign_id": cam.id})
        intent.with_client_id(self.algo, sym, "exit")
        self.ledger.record_order(
            client_order_id=intent.client_order_id, symbol=sym, side=side.value,
            size=cam.contracts, order_type=order_type.value,
            limit_price=float(intent.limit_price) if intent.limit_price else None,
            reduce_only=True, product_id=rt.product.product_id, status="new",
            meta={"purpose": "exit", "campaign_id": cam.id, "reason": reason})
        res = await self.exec.place(intent)
        self.ledger.record_order(
            client_order_id=intent.client_order_id, symbol=sym, side=side.value,
            size=cam.contracts, order_type=order_type.value,
            limit_price=float(intent.limit_price) if intent.limit_price else None,
            reduce_only=True, product_id=rt.product.product_id,
            filled_size=int(res.filled_size or 0),
            avg_fill_price=float(res.avg_fill_price) if res.avg_fill_price else None,
            status="open" if res.accepted else "rejected",
            meta={"purpose": "exit", "campaign_id": cam.id, "reason": reason})
        self.events.log("exit", f"exit requested: {reason}",
                        symbol=sym,
                        features={"reason": reason, "intent": _intent_dump(intent)})
        self.ledger.update_campaign(cam.id, meta=campaign_to_meta(cam))

    def _lot_close_size_provisional(self) -> int:  # pragma: no cover - doc
        return 0

    async def _handle_exit_fill(
        self, rt: SymbolRuntime, campaign_id: int | None, fill: FillEvent,
        reason: str = "exchange_tp", partial: str = "",
    ) -> None:
        sym = rt.symbol
        if campaign_id is None:
            return
        lots = self.ledger.open_lots(sleeve=self.sleeve, symbol=sym)
        remaining = int(fill.size)
        self._close_lots_lifo(lots, float(fill.price), fill.client_order_id, remaining)
        cam = rt.campaign
        realized = self._realized_from_lots(
            self.ledger.open_lots(sleeve=self.sleeve, symbol=sym),
            float(fill.price), lots)
        open_now = len(
            [l for l in self.ledger.open_lots(sleeve=self.sleeve, symbol=sym)])
        if open_now == 0:
            if cam is not None:
                cam.exit_reason = reason
                cam.exit_ts = fill.ts
            self.ledger.close_campaign(campaign_id, close_reason=reason)
            rt.campaign = None
            rt.campaign_id = None
            state = self.sm.state_of(sym)
            if state not in ("EXITING",):
                self._safe_transition(sym, "EXITING", {"reason": reason})
            self.sm.transition(sym, "COOLDOWN", features={"reason": reason,
                                                         "realized": realized})
            # queue cooldown
            rt.cooldown_bars_left = int(self.cfg.get("cooldown_bars", 6))
            self.events.log("exit", f"campaign closed: {reason}", symbol=sym,
                            features={"realized": realized, "reason": reason})
        else:
            # partial close (tp_close_fraction < 1): stay in MANAGING/BUILDING
            self.events.log("exit", "partial profit booking", symbol=sym,
                            features={"closed": int(fill.size), "reason": reason})

    def _close_lots_lifo(
        self, lots: list[dict[str, Any]], price: float,
        exit_client_oid: str, size_budget: int,
    ) -> None:
        """Close open lots NEWEST-first (strict LIFO) until size_budget units
        are closed."""
        lots_desc = list(reversed(lots))
        for lot in lots_desc:
            if size_budget <= 0:
                break
            contracts = int(lot["contracts"])
            self.ledger.close_lot(lot["id"], price, exit_client_oid=exit_client_oid)
            size_budget -= contracts

    def _realized_from_lots(
        self, lots_after: list[dict[str, Any]], price: float,
        closed_before: list[dict[str, Any]],
    ) -> float:
        """Sum of realised pnl of the closed lots (entry_price vs exit price)."""
        closed_ids = {l["id"] for l in closed_before} - {l["id"] for l in lots_after}
        out = 0.0
        for lot in closed_before:
            if lot["id"] in closed_ids:
                sign = 1.0 if lot["side"].lower() == "buy" else -1.0
                out += sign * (price - float(lot.get("entry_price") or 0.0)) * int(lot["contracts"])
        return out

    async def _cancel_unfilled_rungs(self, rt: SymbolRuntime) -> None:
        cam = rt.campaign
        if cam is None:
            return
        for rung in cam.unfilled_rungs:
            if rung.client_order_id:
                await self._cancel(rung.client_order_id, rt.symbol)
            rung.status = "cancelled"
        self.ledger.update_campaign(cam.id, meta=campaign_to_meta(cam))

    async def _cancel(self, client_order_id: str, symbol: str) -> bool:
        if not client_order_id or self.exec is None:
            return False
        try:
            return await self.exec.cancel(client_order_id, symbol)
        except Exception as e:  # noqa: BLE001
            self.events.log("info", f"cancel failed {client_order_id}: {e}",
                            symbol=symbol)
            return False

    def _safe_transition(self, symbol: str, new: str, features: dict | None = None) -> None:
        if self.sm.can(symbol, new):
            self.sm.transition(symbol, new, features)

    # -------------------------------------------------------------- tickers
    async def on_ticker(self, symbol: str, ticker: Ticker) -> None:
        rt = self.runtime.get(symbol)
        if rt is None:
            return
        rt.last_ticker = ticker
        liq = self._liquidity_check(rt, ticker)
        rt.liquidity_block = liq[1] if not liq[0] else None
        cam = rt.campaign
        if cam is not None and cam.fills:
            mark = float(ticker.mark_price) if ticker.mark_price is not None else (
                float(ticker.best_bid) if (ticker.best_bid and cam.direction == "long")
                else float(ticker.best_ask) if (ticker.best_ask and cam.direction == "short")
                else None)
            if mark is not None:
                # software backup TP check on every mark tick (spec 2.9)
                pnl = cam.unrealised_pnl(mark)
                margin = used_margin_contracts(
                    cam.contracts, cam.avg_entry, cam.leverage,
                    cam.contract_value)
                fraction = float(self.cfg.get("tp_close_fraction", 1.0))
                trailing_active = cam.trailing
                if target_hit(pnl, margin, cam.target_pct_of_margin) and not trailing_active:
                    if fraction >= 1.0:
                        await self._request_exit(rt, "profit_target")
                    else:
                        await self._partial_tp(rt, mark)
                elif cam.trailing:
                    # trail the remainder with trail_atr_mult x ATR(1H)
                    mult = self.cfg.get("trail_atr_mult")
                    if mult:
                        new_sl = trailing_stop_price(
                            mark, rt.atr1h, float(mult), cam.direction,
                            cam.sl_price if cam.direction == "long" else None) \
                            if cam.direction == "long" else trailing_stop_price(
                                mark, rt.atr1h, float(mult), cam.direction,
                                cam.sl_price)
                        cam.sl_price = float(new_sl)
                        await self._send_bracket(rt, "trailing_update")
            # liquidation-distance monitor (spec 2.11)
            await self._liq_distance_check(rt, ticker)

    async def _partial_tp(self, rt: SymbolRuntime, mark: float) -> None:
        cam = rt.campaign
        assert cam is not None
        sym = rt.symbol
        fraction = float(self.cfg.get("tp_close_fraction", 1.0))
        n = int(math.floor(cam.contracts * fraction))
        n = min(n, cam.contracts)
        if n <= 0:
            await self._request_exit(rt, "profit_target")
            return
        side = Side.SELL if cam.direction == "long" else Side.BUY
        tick = float(rt.product.tick_size)
        px = (mark - 2 * tick) if cam.direction == "long" else (mark + 2 * tick)
        intent = OrderIntent(
            algo=self.algo, product_symbol=sym, product_id=rt.product.product_id,
            side=side, size=n, order_type=OrderType.LIMIT,
            limit_price=Decimal(str(round(px / tick) * tick)),
            time_in_force=TimeInForce.IOC, post_only=False, reduce_only=True,
            purpose="lot_tp", reason="partial profit booking", meta={"campaign_id": cam.id})
        intent.with_client_id(self.algo, sym, "lot_tp")
        self.ledger.record_order(
            client_order_id=intent.client_order_id, symbol=sym, side=side.value,
            size=n, order_type="limit_order",
            limit_price=float(intent.limit_price),
            reduce_only=True, product_id=rt.product.product_id, status="new",
            meta={"purpose": "lot_tp_partial", "campaign_id": cam.id})
        await self.exec.place(intent)
        cam.trailing = True
        self.ledger.update_campaign(cam.id, meta=campaign_to_meta(cam))
        self.events.log("exit", "partial TP close", symbol=sym,
                        features={"size": n, "fraction": fraction})

    async def _liq_distance_check(self, rt: SymbolRuntime, ticker: Ticker) -> None:
        cam = rt.campaign
        if cam is None or rt.atr4h <= 0:
            return
        pos = await self.exec.position(rt.symbol)
        if pos is None:
            return
        mark = float(ticker.mark_price or ticker.best_bid or 0)
        if not self.portfolio.liquidation_risk(pos, mark, rt.atr4h):
            return
        await self._cancel_unfilled_rungs(rt)
        n = cam.contracts // 2
        if cam is not None and n > 0:
            side = Side.SELL if cam.direction == "long" else Side.BUY
            intent = OrderIntent(
                algo=self.algo, product_symbol=rt.symbol,
                product_id=rt.product.product_id, side=side, size=n,
                order_type=OrderType.MARKET, time_in_force=TimeInForce.IOC,
                post_only=False, reduce_only=True, purpose="deleverage",
                reason="mark within 2x ATR(4H) of liquidation price -> halve",
                meta={"campaign_id": cam.id})
            intent.with_client_id(self.algo, rt.symbol, "deleverage")
            await self.exec.place(intent)
            self.events.log("risk_veto", "liquidation-distance halve triggered",
                            symbol=rt.symbol, features={"half": n, "mark": mark})

    # ---------------------------------------------------------------- timer
    async def on_timer(self) -> None:
        if self.exec is None:
            return
        now = self.now_fn()
        # portfolio checks (equity anchors: daily loss, drawdown halt)
        account = await self.exec.account()
        equity = float(account.equity or 0.0)
        actions = self.portfolio.on_equity_tick(equity, now)
        for act in actions:
            if act == "halt":
                self._halted = True
                self.events.log("halt", "portfolio drawdown halt",
                                features={"reason": self.portfolio.state.halt_reason})
                for sym, rt in self.runtime.items():
                    if rt.campaign is not None:
                        await self._request_exit(rt, "max_drawdown_halt")
            self.events.log("info", f"portfolio action {act}",
                            features={"equity": equity})
        for sym, rt in self.runtime.items():
            cam = rt.campaign
            # entry timeout logic
            await self._entry_timeout_check(rt, now)
            if cam is None:
                continue
            # time stop (spec 2.9)
            if time_stop_hit(cam.created_ts, now,
                             float(self.cfg.get("max_campaign_days", 14))):
                await self._request_exit(rt, "time_stop")
                continue
            # rung TTL: cancel resting rungs when campaign age > rung_ttl_hours
            ttl_hours = float(self.cfg.get("rung_ttl_hours", 72))
            if cam.created_ts:
                try:
                    cts = cam.created_ts if isinstance(cam.created_ts, datetime) \
                        else datetime.fromisoformat(str(cam.created_ts))
                    if cts.tzinfo is None:
                        cts = cts.replace(tzinfo=timezone.utc)
                    if now - cts > timedelta(hours=ttl_hours) and cam.unfilled_rungs:
                        await self._cancel_unfilled_rungs(rt)
                        self.events.log("info", "rung TTL -> unfilled rungs cancelled",
                                        symbol=sym, features={"ttl_hours": ttl_hours})
                except (TypeError, ValueError):
                    pass
            # margin utilisation cap -> cancel unfilled rungs (spec 2.7)
            open_camps = self.ledger.get_campaigns()
            used = self._sum_open_margin(open_camps)
            if equity > 0 and used / equity * 100.0 >= float(
                    self.cfg.get("max_total_margin_pct", 40.0)) and cam.unfilled_rungs:
                await self._cancel_unfilled_rungs(rt)
                self.events.log("risk_veto", "margin cap -> rungs cancelled",
                                symbol=sym, features={"used": used, "equity": equity})

    # ------------------------------------------------------------- reconcile
    async def reconcile(self) -> list[str]:
        """Exchange net position vs DB lots/campaigns (spec 3.3)."""
        mismatches: list[str] = []
        for sym, rt in self.runtime.items():
            pos = await self.exec.position(sym)
            ex_net = int(pos.net_size) if pos is not None else 0
            db_net = self.ledger.sum_signed_contracts(sym, sleeve=self.sleeve)
            if ex_net != db_net:
                cams = self.ledger.get_campaigns(symbol=sym)
                open_cams = [c for c in cams if (c.get("state") or "").upper() != "CLOSED"]
                msg = (f"{sym}: exchange net {ex_net} != ledger lots {db_net} "
                       f"(open campaigns: {len(open_cams)})")
                mismatches.append(msg)
                self.events.log("reconcile", "MISMATCH " + msg, symbol=sym,
                                features={"exchange": ex_net, "db": db_net,
                                          "open_campaigns": [c["id"] for c in open_cams]})
            else:
                self.events.log("reconcile",
                                f"{sym}: exchange net {ex_net} == ledger lots {db_net}",
                                symbol=sym,
                                features={"exchange": ex_net, "db": db_net})
        self.events.log("reconcile", "reconcile pass done",
                        features={"mismatches": mismatches})
        return mismatches

    # ----------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "algo": self.name,
            "symbols": {}
        }
        for sym, rt in self.runtime.items():
            cam = rt.campaign
            out["symbols"][sym] = {
                "state": self.sm.state_of(sym),
                "zones": [z.features() for z in rt.zones[:5]],
                "structure": rt.structure.features_dict(),
                "atr1h": rt.atr1h,
                "campaign": campaign_to_meta(cam) if cam else None,
            }
        out["portfolio"] = {
            "halted": self._halted,
            "state": {
                "equity": self.portfolio.state.equity,
                "peak_equity": self.portfolio.state.peak_equity,
                "day_anchor_equity": self.portfolio.state.day_anchor_equity,
                "halt_reason": self.portfolio.state.halt_reason,
            },
        }
        return out


# -------------------------------------------------------------- small helpers
def _intent_dump(intent: OrderIntent) -> dict[str, Any]:
    d = intent.model_dump(mode="json")
    return d
