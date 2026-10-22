"""Event-driven backtester (spec 3.6, both strategies share one engine).

Drives a StrategyPort over synthetic candle history through a BacktestPort
ExecutionPort implementation, so the strategy code is unchanged versus live.

No lookahead: indicators inside strategies must be computed on CLOSED candles
only; the engine only publishes closed candles, and higher-timeframe candles
are only emitted once fully formed.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from core.models import (
    AccountSnapshot,
    Candle,
    EventNote,
    FillEvent,
    OrderIntent,
    OrderRecord,
    OrderResult,
    OrderStatus,
    OrderType,
    PositionSnapshot,
    ProductInfo,
    Side,
    Ticker,
    now_utc,
)
from core.ports import ExecutionPort, StrategyPort
from backtest.fills import FillSimulator
from backtest.data import resample
from core.models import Candle

HIGHER_TFS = ["15m", "1H", "4H", "1D"]


def bucket_closes_with(candle: Candle, step_seconds: int, base_res: str = "1m") -> bool:
    """True when this base candle is the LAST constituent of its HTF bucket."""
    base_step = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "4H": 14400}.get(base_res, 60)
    ts = int(candle.ts.timestamp())
    return ts % step_seconds == step_seconds - base_step

@dataclass
class BacktestOrder:
    record: OrderRecord
    intent: OrderIntent


@dataclass
class BacktestProduct(ProductInfo):
    pass


@dataclass
class EquityPoint:
    ts: datetime
    equity: float
    used_margin: float
    gross_notional: float


class BacktestPort(ExecutionPort):
    """Simulated exchange: fills from candles, positions netted, fees+funding."""

    def __init__(
        self,
        products: dict[str, ProductInfo],
        initial_equity: Decimal,
        candles: dict[str, list[Candle]],
        funding_bps_per_8h: float = 1.0,
    ):
        self.products_ = dict(products)
        self.initial_equity = initial_equity
        self.equity = initial_equity
        self.realized_pnl = Decimal("0")
        self.candles_ = candles  # symbol -> ordered base-resolution candles
        # serve warmup requests for higher timeframes by resampling (no lookahead)
        for sym, series in list(candles.items()):
            if series:
                for tf in HIGHER_TFS:
                    if tf != series[0].resolution and not any(
                        c.resolution == tf for c in series
                    ):
                        self.candles_.setdefault(
                            f"__resampled__{sym}__{tf}", resample(series, tf)
                        )
        self.resting: dict[str, BacktestOrder] = {}
        self.fills: list[FillEvent] = []
        self.sim = FillSimulator(
            tick_sizes={
                s: p.tick_size for s, p in products.items()
            }
        )
        self.funding_bps_per_8h = funding_bps_per_8h
        self.positions: dict[str, int] = {}  # symbol -> signed contracts
        self._last_mark: dict[str, Decimal] = {}
        self.equity_curve: list[EquityPoint] = []
        self._next_fill_hooks: list = []

    # ----- bookkeeping -----------------------------------------------------
    def _mark(self, symbol: str, candle: Candle) -> Decimal:
        px = Decimal(str(candle.close))
        self._last_mark[symbol] = px
        return px

    def record_equity(self, ts: datetime) -> None:
        used, gross = Decimal("0"), Decimal("0")
        for sym, size in self.positions.items():
            if size == 0:
                continue
            p = self.products_[sym]
            mark = self._last_mark.get(sym, Decimal("0"))
            notional = abs(Decimal(size)) * mark * p.contract_value
            gross += notional
            used += notional / Decimal(str(p.default_leverage or "1"))
        self.equity_curve.append(EquityPoint(ts, float(self.equity), float(used), float(gross)))

    # ----- ExecutionPort ---------------------------------------------------
    async def bootstrap(self) -> None:
        return None

    async def product(self, symbol: str) -> Optional[ProductInfo]:
        return self.products_.get(symbol)

    async def products(self) -> list[ProductInfo]:
        return list(self.products_.values())

    async def ticker(self, symbol: str) -> Optional[Ticker]:
        px = self._last_mark.get(symbol)
        if px is None:
            return None
        p = self.products_[symbol]
        return Ticker(
            symbol=symbol,
            mark_price=px,
            best_bid=px - p.tick_size,
            best_ask=px + p.tick_size,
        )

    async def candles(self, symbol: str, resolution: str, start: int, end: int) -> list[Candle]:
        data = self.candles_.get(symbol, [])
        if resolution != "1m":
            data = self.candles_.get(f"__resampled__{symbol}__{resolution}", []) or resample(data, resolution)
        return [c for c in data if start <= c.ts.timestamp() < end and c.resolution == resolution]

    async def account(self) -> AccountSnapshot:
        positions = []
        for sym, size in self.positions.items():
            if size == 0:
                continue
            p = self.products_[sym]
            mark = self._last_mark.get(sym)
            positions.append(
                PositionSnapshot(
                    symbol=sym,
                    product_id=p.product_id,
                    net_size=size,
                    mark_price=mark,
                    margin=(Decimal(abs(size)) * (mark or Decimal("0")) * p.contract_value)
                    / Decimal(str(p.default_leverage or "1")),
                )
            )
        return AccountSnapshot(
            equity=self.equity,
            used_margin=sum((p.margin or Decimal("0") for p in positions), Decimal("0")),
            positions=positions,
            realized_pnl=self.realized_pnl,
            wallet_balance=self.equity - self.realized_pnl,
        )

    async def position(self, symbol: str) -> Optional[PositionSnapshot]:
        snap = await self.account()
        for p in snap.positions:
            if p.symbol == symbol:
                return p
        return None

    async def open_orders(self, symbol: Optional[str] = None) -> list[OrderRecord]:
        return [
            o.record
            for o in self.resting.values()
            if symbol is None or o.record.symbol == symbol
        ]

    async def place(self, intent: OrderIntent) -> Any:
        rec = OrderRecord(
            client_order_id=intent.client_order_id,
            symbol=intent.product_symbol,
            side=intent.side,
            size=intent.size,
            status=OrderStatus.OPEN,
            order_type=intent.order_type.value,
            limit_price=intent.limit_price,
            reduce_only=intent.reduce_only,
            product_id=intent.product_id,
        )
        bt = BacktestOrder(rec, intent)
        if intent.order_type is OrderType.MARKET:
            # fill at the close of the candle just processed + slippage
            px = self._last_mark.get(intent.product_symbol)
            if px is None:
                return None
            fill_px = self.sim.market_fill_price(intent.side, px, self.products_[intent.product_symbol].tick_size)
            self._fill(bt, int(intent.size), fill_px)
        else:
            self.resting[intent.client_order_id] = bt
        # paper/live ports return OrderResult (port contract, spec 3.2);
        # backtest must match — the strategy reads res.filled_size etc.
        return OrderResult(
            client_order_id=rec.client_order_id,
            accepted=True,
            order_id=rec.client_order_id,
            status=rec.status,
            filled_size=int(rec.filled_size),
            avg_fill_price=getattr(rec, "avg_fill_price", None),
        )

    async def cancel(self, client_order_id: str, symbol: str) -> bool:
        return self.resting.pop(client_order_id, None) is not None or True

    async def reconcile(self, ledger_state: dict[str, Any]) -> list[str]:
        mismatches = []
        for sym, expected in ledger_state.items():
            actual = self.positions.get(sym, 0)
            if int(expected) != actual:
                mismatches.append(f"{sym}: ledger {expected} != exchange {actual}")
        return mismatches

    def note(self, note: EventNote) -> None:
        pass  # backtester can log to csv later

    # ----- simulation core --------------------------------------------------
    def _fee(self, symbol: str, notional: Decimal, reduce_only: bool, post_only: bool) -> Decimal:
        p = self.products_[symbol]
        rate = p.maker_commission_rate if post_only else p.taker_commission_rate
        return notional * rate

    def _fill(self, bt: BacktestOrder, qty: int, price: Decimal) -> None:
        p = self.products_[bt.record.symbol]
        signed = qty if bt.record.side is Side.BUY else -qty
        self.positions[bt.record.symbol] = self.positions.get(bt.record.symbol, 0) + signed
        notional = Decimal(qty) * price * p.contract_value
        fee = self._fee(bt.record.symbol, notional, bt.record.reduce_only, bt.intent.post_only)
        self.equity -= fee
        if bt.intent.reduce_only:
            self.realized_pnl += Decimal("0")  # PnL attribution per closed lot lives in strategy ledger
        fill = FillEvent(
            client_order_id=bt.record.client_order_id,
            symbol=bt.record.symbol,
            product_id=p.product_id,
            side=bt.record.side,
            size=qty,
            price=price,
            fee=fee,
            reduce_only=bt.record.reduce_only,
        )
        self.fills.append(fill)
        bt.record.filled_size += qty
        if bt.record.filled_size >= bt.record.size:
            bt.record.status = OrderStatus.FILLED
            self.resting.pop(bt.record.client_order_id, None)
        else:
            bt.record.status = OrderStatus.PARTIAL

    def process_candle(self, candle: Candle) -> list[FillEvent]:
        """After publishing a closed candle to the strategy, simulate fills."""
        self._mark(candle.symbol, candle)
        out: list[FillEvent] = []
        # SL-before-TP within-one-candle conservatism lives in each order's trigger
        for oid in list(self.resting.keys()):
            bt = self.resting[oid]
            res = self.sim.limit_fill(bt.record, candle, self.products_[bt.record.symbol].tick_size)
            if res.filled:
                self._fill(bt, res.quantity, res.fill_price)
                out.append(self.fills[-1])
        # simple funding accrual every 480 one-minute candles (8h)
        if int(candle.ts.timestamp()) % 480 == 0:
            for sym, size in self.positions.items():
                p = self.products_[sym]
                notional = Decimal(abs(size)) * Decimal(str(candle.close)) * p.contract_value
                funding = notional * Decimal(str(self.funding_bps_per_8h)) / Decimal("10000")
                self.equity -= funding if size > 0 else -funding
        return out


async def run_backtest(
    strategy: StrategyPort,
    port: BacktestPort,
    candle_stream: list[Candle],
    report_dir: Optional[Path] = None,
    extra_tfs: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Replay base-resolution candles, feed strategy, simulate fills, return stats.

    Higher-timeframe candles are published only when their bucket is fully
    closed (last constituent bar) — strategies see closed bars only (spec 3.6).
    """
    await port.bootstrap()
    await strategy.bootstrap(port)
    last_status_minute = -1
    base_res = candle_stream[0].resolution if candle_stream else "1m"
    tfs = [tf for tf in (extra_tfs or HIGHER_TFS) if tf != base_res]
    aggregates: dict[tuple[str, str], list[Candle]] = {}
    published_upto: dict[tuple[str, str], int] = {}

    def htf_closes_at(candle: Candle, tf: str) -> bool:
        step = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "4H": 14400, "1D": 86400}.get(tf, None)
        return step is not None and bucket_closes_with(candle, step, base_res)

    for candle in candle_stream:
        await strategy.on_candle_close(candle)
        fills_now = port.process_candle(candle)
        for fill in fills_now:
            await strategy.on_fill(fill)
        # accumulate and publish completed HTF buckets after the closing base bar
        for tf in tfs:
            step = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "4H": 14400, "1D": 86400}.get(tf)
            if step is None:
                continue
            key = (candle.symbol, tf)
            aggregates.setdefault(key, []).append(candle)
            if bucket_closes_with(candle, step, base_res):
                acc = aggregates[key]
                from datetime import datetime as _dt

                htf = Candle(
                    symbol=candle.symbol,
                    resolution=tf,
                    ts=acc[0].ts,
                    open=acc[0].open,
                    high=max(x.high for x in acc),
                    low=min(x.low for x in acc),
                    close=acc[-1].close,
                    volume=sum(x.volume for x in acc),
                    closed=True,
                )
                aggregates[key] = []
                await strategy.on_candle_close(htf)
        for fill in list(port.fills[len(port.fills) - len(fills_now) if fills_now else len(port.fills):]):
            # ensure strategy sees resting-limit fills too (already emitted above
            # via process_candle return; this pass is belt-and-braces safety)
            pass
        minute = int(candle.ts.timestamp() // 60)
        if minute != last_status_minute:
            await strategy.on_timer()
            last_status_minute = minute
            port.record_equity(candle.ts)
    summary = {
        "initial_equity": float(port.initial_equity),
        "final_equity": float(port.equity),
        "fills": len(port.fills),
        "equity_curve": [
            {"ts": e.ts.isoformat(), "equity": e.equity, "used_margin": e.used_margin}
            for e in port.equity_curve
        ],
    }
    if report_dir:
        import json

        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "equity_curve.csv").write_text(
            "\n".join(
                [",".join(["ts", "equity", "used_margin"])]
                + [
                    f"{e.ts.isoformat()},{e.equity},{e.used_margin}"
                    for e in port.equity_curve
                ]
            )
        )
        (report_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary
