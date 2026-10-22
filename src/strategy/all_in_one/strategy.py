"""AllInOneStrategy — "All-in-One Delta Trades — Multi-Strategy Portfolio".

Composition (specs 2.2-2.11, 3.3):
- One PortfolioManager owns allocation + global risk.
- Four sleeves share one ExecutionPort, one LIFOLedger and one regime engine:
    ETH_CC (covered call, long), BTC_CP (covered put, short),
    GOLD (XAUTUSD + PAXGUSD, CC_MODE / CP_MODE per regime),
    STOCKS (accumulate -> distribute on US-equity perps).
- Scheduling: daily 00:00 UTC jobs (regime + allocation) -- i.e. every 1D
  close; 4H-close jobs; 1-minute loop (on_timer); hourly cross-margin check
  + reconciliation; options cycle every 15 minutes (inside on_timer).
- Startup / post-reconnect reconciliation: sum(open lots, signed) == the
  exchange net position per symbol; any mismatch HALTs that sleeve.
- The strategy NEVER calls the exchange directly (ExecutionPort only).
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.events import EventLogger, setup_logging
from core.ledger import LIFOLedger
from core.models import Candle, Event as EvKind, FillEvent, Ticker, now_utc
from core.ports import ExecutionPort, StrategyPort
from core.state import DeltaSchema, now_iso
from strategy.all_in_one.allocation import Allocator
from strategy.all_in_one.defaults import get, merge_defaults
from strategy.all_in_one.portfolio import PortfolioManager
from strategy.all_in_one.regime import HistoryStore, RegimeEngine
from strategy.all_in_one.sleeve_base import SleeveStatus
from strategy.all_in_one.sleeve_btc_cp import BtcCpSleeve
from strategy.all_in_one.sleeve_eth_cc import EthCcSleeve
from strategy.all_in_one.sleeve_gold import GoldSleeve
from strategy.all_in_one.sleeve_stocks import StocksSleeve


class AllInOneStrategy(StrategyPort):
    name = "all_in_one"
    algo = "aio"

    def __init__(
        self,
        cfg_all_in_one: Optional[dict[str, Any]] = None,
        state_dir: str = "state",
        log_dir: str = "logs",
    ) -> None:
        self.cfg = merge_defaults(cfg_all_in_one)
        self.state_dir = state_dir
        self.log_dir = log_dir
        self.exec_port: Optional[ExecutionPort] = None
        self.store = HistoryStore()
        self.regime = RegimeEngine(self.cfg, self.store)
        self.ledger: Optional[LIFOLedger] = None
        self.events: Optional[EventLogger] = None
        self.portfolio: Optional[PortfolioManager] = None
        self.sleeves: dict[str, Any] = {}
        self.resolved_symbols: dict[str, list[str]] = {}
        # clock used on_daily_close / portfolio re-checks; override in tests
        self._now = now_utc
        self._last_account: Optional[Any] = None
        self._last_equity: Optional[float] = None
        self._last_hour: Optional[str] = None
        self._last_options: Optional[str] = None
        self._started = False

    # ------------------------------------------------------------- bootstrap
    async def bootstrap(self, exec_port: ExecutionPort) -> None:
        self.exec_port = exec_port
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)
        self.ledger = LIFOLedger(os.path.join(self.state_dir, "aio_state.sqlite"), prefix="")
        # events share the same SQLite handle/schema (events, spec 3.5)
        self.events = EventLogger(self.ledger.db, algo=self.algo)
        try:
            from strategy.all_in_one.defaults import get as _get

            _json_path = os.path.join(self.log_dir, "aio_events.jsonl")
            self.log_handle = open(_json_path, "a", encoding="utf-8")  # noqa: SIM115
            self._json_log_path = _json_path
        except Exception:
            self._json_log_path = None

        # warm-up: resolve products, load candles, rebuild state from the DB
        universe = await self._resolve_universe(exec_port)
        await self._build_sleeves(exec_port, universe)
        await self._warmup_candles(exec_port)
        # reconcile BEFORE trading is allowed (spec 3.3)
        mismatches = await self.reconcile()
        if mismatches:
            self.note_event(f"bootstrap reconcile mismatches: {mismatches}",
                            kind="reconcile")
        await self._restore_tps()
        try:
            account = await exec_port.account()
            self._last_account = account
            self._last_equity = float(account.equity or 0)
            mode = (account.raw or {}).get("margin_mode")
            if mode is not None:
                self.portfolio.set_margin_mode(
                    str(mode).lower() == str(get(self.cfg, "margin_mode_required", "cross")).lower(),
                    detail=str(mode),
                )
        except Exception as e:
            self.note_event(f"initial account fetch failed: {e}", kind="halt")
        self._apply_regime_statuses()
        self._maybe_alloc()
        self._started = True
        self.note_event("bootstrap complete")

    def note_event(self, message: str, kind: str = "info",
                   symbol: Optional[str] = None, features: dict | None = None) -> None:
        if self.events is None:
            return
        try:
            self.events.log(kind=EvKind(kind).value, message=message,
                            symbol=symbol, features=features or {},
                            algo=self.algo)
        except Exception:
            pass

    # ------------------------------------------------------------- universe
    async def _resolve_universe(self, exec_port: ExecutionPort) -> dict[str, list[str]]:
        eth = str(get(self.cfg, "eth_cc.symbol", "ETHUSD"))
        btc = str(get(self.cfg, "btc_cp.symbol", "BTCUSD"))
        gold = [str(s).upper() for s in
                (get(self.cfg, "gold.symbols", ["XAUTUSD", "PAXGUSD"]) or [])]
        max_stocks = int(get(self.cfg, "stocks.max_stock_symbols", 6))
        configured = get(self.cfg, "stocks.stock_symbols", None)
        stocks: list[str] = []
        if configured:
            stocks = [str(s).upper() for s in configured][:max_stocks]
        else:
            try:
                products = await exec_port.products()
            except Exception:
                products = []
            discovered = [p.symbol.upper() for p in products if _is_stock_perp(p.symbol)]
            stocks = discovered[:max_stocks]
        universe = {"eth_cc": [eth], "btc_cp": [btc], "gold": gold, "stocks": stocks}
        self.resolved_symbols = universe
        self.note_event("universe resolved", features={"symbols": universe})
        if not configured and not stocks:
            stock_cfg = get(self.cfg, "stocks.stock_symbols", None)
            self.note_event("stock_symbols [ASK] is null and none discovered live "
                            "-> STOCKS sleeve inactive", kind="skip")
            assert stock_cfg is not None or not stocks  # [ASK] key stays honest
        return universe

    async def _build_sleeves(self, exec_port: ExecutionPort,
                             universe: dict[str, list[str]]) -> None:
        assert self.ledger is not None and self.events is not None
        common = dict(exec_port=exec_port, ledger=self.ledger, events=self.events,
                      cfg=self.cfg, store=self.store, regime=self.regime, algo=self.algo)
        if bool(get(self.cfg, "eth_cc.enabled", True)):
            self.sleeves["ETH_CC"] = EthCcSleeve(**common, symbols=universe["eth_cc"])
        if bool(get(self.cfg, "btc_cp.enabled", True)):
            self.sleeves["BTC_CP"] = BtcCpSleeve(**common, symbols=universe["btc_cp"])
        if bool(get(self.cfg, "gold.enabled", True)):
            self.sleeves["GOLD"] = GoldSleeve(**common, symbols=universe["gold"])
        if bool(get(self.cfg, "stocks.enabled", True)):
            self.sleeves["STOCKS"] = StocksSleeve(**common, symbols=universe["stocks"])
        self.portfolio = PortfolioManager(self.cfg, Allocator(self.cfg), self.events)
        for s in self.sleeves.values():
            s.portfolio = self.portfolio
            if hasattr(s, "warmup"):
                await s.warmup()

    async def _warmup_candles(self, exec_port: ExecutionPort) -> None:
        n1d = int(get(self.cfg, "buffer_candles_1d", 400))
        n4h = int(get(self.cfg, "buffer_candles_4h", 200))
        now = datetime.now(timezone.utc)
        symbols = {s for lst in self.resolved_symbols.values() for s in lst}
        for sym in sorted(symbols):
            try:
                start1 = int((now - timedelta(days=n1d + 5)).timestamp())
                got = await exec_port.candles(sym, "1D", start1, int(now.timestamp()))
                for c in got[-n1d:]:
                    self.store.upsert(c)
                start4 = int((now - timedelta(hours=4 * n4h)).timestamp())
                got4 = await exec_port.candles(sym, "4H", start4, int(now.timestamp()))
                for c in got4[-n4h:]:
                    self.store.upsert(c)
            except Exception as e:
                self.note_event(f"warmup candles failed for {sym}: {e}", kind="halt",
                                symbol=sym)
            self.regime.refresh(sym)
            if sym in gold_symbols(self):
                g = self.sleeves.get("GOLD")
                if g is not None:
                    g.on_daily_close(sym, now)

    # ------------------------------------------------------------- regime
    def _apply_regime_statuses(self) -> None:
        """Regime -> sleeve statuses (spec 2.9 maps). Sleeve stops and
        de-risks are never silently reset while lots remain open."""
        for sleeve in ("ETH_CC", "BTC_CP"):
            s = self.sleeves.get(sleeve)
            if s is None or self.regime.get(s.symbols[0]) is None:
                continue
            if s.status is SleeveStatus.DISTRIBUTE_ONLY and s.lots_open(s.symbols[0]):
                continue
            s.set_status(self.regime.sleeve_status(sleeve, s.symbols[0]), "regime map")
        gold = self.sleeves.get("GOLD")
        if gold is not None:
            for sym in gold.symbols:
                if self.mode_of(sym):
                    gold.set_status(gold.gold_status_map(sym),
                                    f"{sym} mode {gold.mode.get(sym)}")

    def mode_of(self, symbol: str) -> Optional[str]:
        g = self.sleeves.get("GOLD")
        return g.mode.get(symbol) if g is not None and symbol in g.symbols else None

    # --------------------------------------------------------- candle event
    async def on_candle_close(self, candle: Candle) -> None:
        for s in self.sleeves.values():
            if candle.symbol in s.symbols:
                await s.on_candle_close(candle)
        if candle.resolution == "1D":
            # a 1D bar closes exactly at 00:00 UTC -> daily jobs (spec 2.11)
            await self._daily_job(candle.symbol)
        elif candle.resolution == "4H":
            await self._hour_job(candle.symbol)

    async def _daily_job(self, symbol: str) -> None:
        """Daily job at 00:00 UTC: regime + allocation (spec 2.9/2.10)."""
        self.regime.refresh(symbol)
        gold = self.sleeves.get("GOLD")
        if gold is not None and symbol in gold.symbols:
            gold.on_daily_close(symbol, self._now())
        self._apply_regime_statuses()
        self._maybe_alloc()

    async def _hour_job(self, symbol: str) -> None:
        """4H close: re-evaluate the regime (lagged, no lookahead)."""
        self.regime.refresh(symbol)
        self._apply_regime_statuses()

    def _maybe_alloc(self) -> None:
        """Recompute sleeve budgets (changes affect NEW lots only, 2.10)."""
        if self.portfolio is None:
            return
        trends: dict[str, str] = {}
        vols: dict[str, float] = {}
        for s in self.sleeves.values():
            rep = s.symbols[0] if s.symbols else None
            if rep is None:
                continue
            snap = self.regime.get(rep)
            trends[s.name] = snap.trend if snap else "neutral"
            a = s.atr(rep)
            close = snap.close if snap else 0.0
            vols[s.name] = max(a / close, 1e-9) if (a and close > 0) else 1.0
        self.portfolio.allocator.rebalance(
            sleeve_trends=trends, sleeve_vols=vols,
            capital=float(self._last_equity) if self._last_equity else None,
        )

    # ------------------------------------------------------------- events
    async def on_ticker(self, symbol: str, ticker: Ticker) -> None:
        for s in self.sleeves.values():
            if symbol in s.symbols:
                await s.on_ticker(symbol, ticker)

    async def on_fill(self, fill: FillEvent) -> None:
        sym = str(fill.symbol)
        target: Optional[Any] = None
        # option fills first: the sleeve whose hedge tools track that symbol
        for s in self.sleeves.values():
            if getattr(s, "hedge", None) is None:
                continue
            for st in s.hedge.state.values():
                if st.option_symbol == sym or st.pending_oid == fill.client_order_id:
                    target = s
                    break
            if target is not None:
                break
        if target is None:
            for s in self.sleeves.values():
                if sym in s.symbols:
                    target = s
                    break
        if target is None:
            self.note_event(f"fill for unknown symbol {sym}", kind="skip",
                            symbol=sym)
            return
        await target.on_fill(fill)

    # ------------------------------------------------------------- timer
    async def on_timer(self) -> None:
        now = self._now()
        hour = now.strftime("%Y%m%d%H")
        slot15 = hour + f"M{now.minute // 15}"
        # 1-minute loop: risk monitor
        await self._risk_monitor()
        # options cycle every 15 minutes (spec 2.11)
        if slot15 != self._last_options:
            self._last_options = slot15
            for s in self.sleeves.values():
                await s.options_cycle(now)
        # hourly cross-margin + reconciliation checks (spec 2.3 / 3.3)
        if hour != self._last_hour:
            self._last_hour = hour
            await self._hourly_checks()
        await self._de_risk_if_breached()

    async def _risk_monitor(self) -> None:
        if self.exec_port is None or self.portfolio is None:
            return
        try:
            account = await self.exec_port.account()
        except Exception as e:
            self.note_event(f"account fetch failed: {e}", kind="halt")
            return
        self._last_account = account
        self._last_equity = float(account.equity or 0)
        self.portfolio.update(account, self._now())
        if self.portfolio.flatten_requested:
            await self.flatten_all("risk limits (daily loss / drawdown) breached")

    async def _hourly_checks(self) -> None:
        """Cross margin is read by the infra layer; we REACT to it here."""
        if self.exec_port is None or self.portfolio is None:
            return
        try:
            account = await self.exec_port.account()
        except Exception as e:
            self.note_event(f"hourly account fetch failed: {e}", kind="halt")
            return
        self._last_account = account
        self._last_equity = float(account.equity or 0)
        mode = (account.raw or {}).get("margin_mode")
        if mode is not None:
            self.portfolio.set_margin_mode(
                str(mode).lower() == str(get(self.cfg, "margin_mode_required", "cross")).lower(),
                detail=str(mode),
            )
        await self.reconcile()

    async def _de_risk_if_breached(self) -> None:
        if self.portfolio is None or not self.portfolio.risk_breached:
            return

        async def on_done() -> None:
            await self._risk_monitor()

        await self.portfolio.derisk_newest_first(
            list(self.sleeves.values()), on_done=on_done,
        )

    # ------------------------------------------------------------ reconcile
    async def reconcile(self) -> list[str]:
        """sum(open lots signed) == exchange net position per symbol (3.3)."""
        if self.ledger is None or self.exec_port is None:
            return []
        symbols = sorted({s for lst in self.resolved_symbols.values() for s in lst})
        ledger_state = {sym: self.ledger.sum_signed_contracts(sym) for sym in symbols}
        mism: list[str] = []
        hook = getattr(self.exec_port, "reconcile", None)
        if callable(hook):
            try:
                mism = list(await hook(ledger_state) or [])
            except Exception as e:
                self.note_event(f"exec_port.reconcile hook failed: {e}",
                                kind="reconcile")
        for sym, signed in ledger_state.items():
            try:
                pos = await self.exec_port.position(sym)
            except Exception:
                pos = None
            net = int(pos.net_size) if pos is not None else 0
            if net != signed:
                mism.append(f"{sym}: ledger={signed} exchange={net} (HALT owning sleeve)")
        for m in mism:
            sym = m.strip().split(":")[0].split(" ")[0]
            for s in self.sleeves.values():
                if sym in s.symbols:
                    s.halt(m)
                    if self.portfolio is not None:
                        self.portfolio.halt_sleeve(s.name, m)
        return mism

    async def _restore_tps(self) -> None:
        for s in self.sleeves.values():
            await s.restore()

    # ------------------------------------------------------------- flat
    async def flatten_all(self, reason: str) -> None:
        """Global flatten / kill switch handling: close everything newest-first."""
        for s in self.sleeves.values():
            if not s.halted:
                s.halt(reason)
            for sym in s.symbols:
                for _ in range(get(self.cfg, "flatten_safety_loop", 50)):
                    if not s.lots_open(sym):
                        break
                    await s.close_newest_market(sym, reason, purpose="flatten")
        if self.portfolio is not None:
            self.portfolio.clear_flatten()

    def status(self) -> dict[str, Any]:
        return {
            "strategy": self.name,
            "universe": dict(self.resolved_symbols),
            "sleeves": {nm: s.status() for nm, s in self.sleeves.items()},
            "portfolio": self.portfolio.status() if self.portfolio else None,
            "regime": {
                sym: (snap.model_dump(mode="json") if snap is not None else None)
                for sym, snap in self.regime.snapshots.items()
            },
        }


def _is_stock_perp(symbol: str) -> bool:
    """Stock perps look like NVDAXUSD / TSLAXUSD / AAPLXUSD (spec 1.6)."""
    s = symbol.upper()
    if not s.endswith("XUSD"):
        return False
    return bool(re.fullmatch(r"[A-Z]{2,6}", s[:-4]))


def gold_symbols(strategy: "AllInOneStrategy") -> set[str]:
    g = strategy.sleeves.get("GOLD")
    return set(g.symbols) if g is not None else set()
