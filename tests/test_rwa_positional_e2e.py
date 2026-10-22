"""End-to-end (prompt A 2.12 / 3.3): warmup -> sweep -> entry -> rung2 fill ->
tick to target -> campaign close; lot table + events + order sequence + the
IDLE->ARMED->TRIGGERED->BUILDING->MANAGING->EXITING->COOLDOWN state path.

Uses ONLY a local in-memory execution-port stub — never src/backtest or src/live.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.models import (
    AccountSnapshot,
    Candle,
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
    TimeInForce,
)
from core.ports import ExecutionPort
from core.state import DeltaSchema
from strategy.rwa_positional import RwaPositionalStrategy

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


# ----------------------------------------------------------------------------
# market data builders
# ----------------------------------------------------------------------------
def _c(res: str, i: int, minutes: int, o: float, h: float, l: float, cl: float,
       v: float = 5.0) -> Candle:
    return Candle(symbol="XAUTUSD", resolution=res,
                  ts=BASE + timedelta(minutes=minutes * i),
                  open=o, high=h, low=l, close=cl, volume=v, closed=True)


def gen_1h() -> list[Candle]:
    """1H warmup (~1.2 wide bars) with two dips touching level 100."""
    out = []
    for i in range(40):
        m = 100.2 if i in (10, 25) else 101.0
        out.append(_c("1H", i, 60, o=m - 0.1, h=m + 0.6, l=m - 0.6, cl=m + 0.15))
    return out


def gen_4h() -> list[Candle]:
    """Rising highs (112.6@10, 115.2@22, 115.9@31) + rising lows (100.5@13,
    110.0@28) => 4H bias bullish."""
    hi = [104.2, 104.0, 104.6, 103.8, 104.4, 105.5, 108.0, 110.2, 111.4, 112.3,
          112.6, 112.5, 109.4, 104.3, 104.1, 105.3, 107.2, 111.4, 112.4, 112.9,
          114.0, 114.8, 115.2, 115.0, 114.6, 114.9, 115.1, 115.3, 115.5, 115.8,
          115.9, 116.1, 115.6, 115.7, 115.8, 115.9, 117.0, 116.4, 116.8, 116.6]
    lo = [102.9, 103.1, 102.7, 100.0, 101.1, 102.9, 104.0, 106.9, 108.4, 109.9,
          111.0, 108.5, 102.2, 100.5, 102.4, 103.1, 104.9, 106.1, 108.5, 109.5,
          110.2, 110.9, 111.4, 111.9, 112.4, 112.9, 110.5, 110.2, 110.0, 110.6,
          111.8, 112.6, 113.0, 111.5, 113.6, 113.8, 114.0, 114.2, 114.4, 114.6]
    out = []
    for i, (h, l) in enumerate(zip(hi, lo)):
        out.append(_c("4H", i, 240, o=l + 0.05, h=h, l=l, cl=h - 0.2))
    return out


def gen_1d() -> list[Candle]:
    return [_c("1D", 0, 1440, o=103.0, h=106.0, l=99.5, cl=104.0),
            _c("1D", 1, 1440, o=104.0, h=108.0, l=100.0, cl=105.0),
            _c("1D", 2, 1440, o=105.0, h=107.0, l=103.0, cl=106.0)]


def gen_15m_warm() -> list[Candle]:
    return [_c("15m", i, 15, o=101.0, h=101.15, l=100.85, cl=101.05, v=10.0)
            for i in range(25)]


# ----------------------------------------------------------------------------
# fake execution port
# ----------------------------------------------------------------------------
class FakeExecPort(ExecutionPort):
    """In-memory ExecutionPort: fixed products/candles, resting post-only
    orders fill when `set_mark` trades through them, bracket TP/SL fire on
    the mark price."""

    wired = True

    def __init__(self, equity: Decimal = Decimal("100000")) -> None:
        self.pmap: dict[str, ProductInfo] = {
            "XAUTUSD": ProductInfo(
                symbol="XAUTUSD", product_id=101,
                contract_type="perpetual_futures",
                tick_size=Decimal("0.1"), contract_value=Decimal("1"),
                contract_unit_currency="XAUT",
                trading_status="operational",
                description="Tether Gold token perp", tags=["rwa", "gold"]),
        }
        self.plist = list(self.pmap.values())
        self.cstore: dict[tuple[str, str], list[Candle]] = {
            ("XAUTUSD", "1H"): gen_1h(),
            ("XAUTUSD", "4H"): gen_4h(),
            ("XAUTUSD", "1D"): gen_1d(),
            ("XAUTUSD", "15m"): gen_15m_warm(),
        }
        self.equity = equity
        self.mark: float = 101.0
        self.spread: float = 0.1
        self.resting: dict[str, dict[str, Any]] = {}
        self.bracket: dict[str, Any] | None = None
        self.fill_queue: list[FillEvent] = []
        self.orders_sent: list[OrderIntent] = []
        self.injected_position: PositionSnapshot | None = None
        self.injected_candles: dict[tuple[str, str], list[Candle]] = {}

    # --- test helpers -------------------------------------------------------
    def set_mark(self, price: float) -> None:
        self.mark = float(price)
        for cid, spec in list(self.resting.items()):
            px = float(spec["limit_price"])
            if spec["side"] == "buy" and self.mark <= px:
                self.resting.pop(cid)
                self._queue(cid, spec["size"], Side.BUY, px)
            elif spec["side"] == "sell" and self.mark >= px:
                self.resting.pop(cid)
                self._queue(cid, spec["size"], Side.SELL, px)
        if self.bracket is not None:
            size = int(self.bracket["size"])
            stamp = len(self.fill_queue)
            if self.bracket["direction"] == "long" and self.mark >= self.bracket["tp"]:
                self._queue(f"exch-tp-{stamp}", size, Side.SELL,
                            float(self.bracket["tp"]), reduce_only=True)
            elif self.bracket["direction"] == "long" and self.mark <= self.bracket["sl"]:
                self._queue(f"exch-sl-{stamp}", size, Side.SELL,
                            float(self.bracket["sl"]), reduce_only=True)
            elif self.bracket["direction"] == "short" and self.mark <= self.bracket["tp"]:
                self._queue(f"exch-tp-{stamp}", size, Side.BUY,
                            float(self.bracket["tp"]), reduce_only=True)
            elif self.bracket["direction"] == "short" and self.mark >= self.bracket["sl"]:
                self._queue(f"exch-sl-{stamp}", size, Side.BUY,
                            float(self.bracket["sl"]), reduce_only=True)

    def _queue(self, cid: str, size: int, side: Side, price: float,
               reduce_only: bool = False) -> None:
        self.fill_queue.append(FillEvent(
            client_order_id=cid, symbol="XAUTUSD", product_id=101,
            side=side, size=int(size), price=Decimal(str(price)),
            ts=datetime.now(timezone.utc), reduce_only=reduce_only))

    async def pump(self, strategy: Any) -> None:
        while self.fill_queue:
            f = self.fill_queue.pop(0)
            await strategy.on_fill(f)

    # --- ExecutionPort API ----------------------------------------------------
    async def bootstrap(self) -> None:
        pass

    async def product(self, symbol: str):
        return self.pmap.get(symbol)

    async def products(self):
        return self.plist

    async def ticker(self, symbol: str):
        bid = self.mark - self.spread / 2
        ask = self.mark + self.spread / 2
        return Ticker(symbol=symbol, mark_price=Decimal(str(self.mark)),
                      best_bid=Decimal(str(round(bid, 4))),
                      best_ask=Decimal(str(round(ask, 4))),
                      turnover_usd=Decimal("1000000"))

    async def candles(self, symbol: str, resolution: str, start: int, end: int):
        return self.injected_candles.get((symbol, resolution),
                                         self.cstore.get((symbol, resolution), []))

    async def account(self) -> AccountSnapshot:
        return AccountSnapshot(equity=self.equity,
                               wallet_balance=self.equity,
                               used_margin=Decimal("0"))

    async def position(self, symbol: str):
        return self.injected_position

    async def open_orders(self, symbol: str | None = None):
        out = []
        for cid, spec in self.resting.items():
            if symbol and spec["symbol"] != symbol:
                continue
            out.append(OrderRecord(
                client_order_id=cid, symbol=spec["symbol"],
                side=Side(spec["side"]), size=spec["size"],
                limit_price=Decimal(str(spec["limit_price"])),
                status=OrderStatus.OPEN))
        return out

    async def place(self, intent: OrderIntent) -> OrderResult:
        self.orders_sent.append(intent)
        cid = intent.client_order_id
        if intent.purpose == "bracket":
            direction = "long" if intent.side == Side.SELL else "short"
            self.bracket = {
                "symbol": intent.product_symbol,
                "direction": direction,
                "sl": float(intent.bracket_stop_loss_price),
                "tp": float(intent.bracket_take_profit_price),
                "size": intent.size,
            }
            return OrderResult(client_order_id=cid, accepted=True,
                               status=OrderStatus.OPEN)
        limit = float(intent.limit_price) if intent.limit_price is not None else None
        if intent.order_type == OrderType.MARKET or (
                intent.reduce_only and limit is not None and (
                    (intent.side == Side.SELL and self.mark >= limit) or
                    (intent.side == Side.BUY and self.mark <= limit))):
            px = float(limit) if limit is not None else self.mark
            self._queue(cid, intent.size, intent.side, px,
                        reduce_only=intent.reduce_only)
            return OrderResult(client_order_id=cid, accepted=True,
                               status=OrderStatus.FILLED,
                               filled_size=intent.size,
                               avg_fill_price=Decimal(str(px)))
        if intent.side == Side.BUY and self.mark <= (limit or 0):
            self._queue(cid, intent.size, intent.side, float(limit))
            return OrderResult(client_order_id=cid, accepted=True,
                               status=OrderStatus.FILLED,
                               filled_size=intent.size,
                               avg_fill_price=Decimal(str(limit)))
        self.resting[cid] = {"symbol": intent.product_symbol,
                             "side": intent.side.value,
                             "size": int(intent.size),
                             "limit_price": float(limit or 0)}
        return OrderResult(client_order_id=cid, accepted=True,
                           status=OrderStatus.OPEN)

    async def cancel(self, client_order_id: str, symbol: str) -> bool:
        self.resting.pop(client_order_id, None)
        return True

    async def reconcile(self, ledger_state: dict[str, Any]) -> list[str]:
        return []


# ----------------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------------
def make_strategy(tmp_path, **cfg_over) -> RwaPositionalStrategy:
    cfg = {
        "symbols": ["XAUTUSD"],
        "energy_scan": False,
        "leverage": 5.0,
        "max_leverage": 10,
        "min_turnover_usd": None,
        "margin_mode": "cross",
        "recommended_margin": None,
        "rungs": 2,
        "rung_weights": [0.5, 0.5],
        "ladder_atr_mult": [0.6],
        "ladder_mode": "against",
        "entry_timeout_s": 20,
        "campaign_margin_pct": 15.0,
        "campaign_risk_pct": 1.0,
        "profit_target_pct_of_margin": 20.0,
        "tp_close_fraction": 1.0,
        "max_campaign_days": 14,
        "cooldown_bars": 6,
        "rung_ttl_hours": 72,
        "warmup_bars": {"4H": 100, "1D": 10, "1H": 100, "15m": 60},
    }
    cfg.update(cfg_over)
    db_path = str(tmp_path / "rwa.db")
    return RwaPositionalStrategy(cfg, db_path=db_path)


def feed_15m(strategy: Any, port: FakeExecPort, i: int, o: float, h: float,
             l: float, cl: float, v: float = 10.0) -> None:
    candle = _c("15m", i, 15, o=o, h=h, l=l, cl=cl, v=v)


async def test_full_campaign_lifecycle(tmp_path):
    """warmup -> sell-side sweep -> rung1 -> rung2 -> TP -> close -> cooldown."""
    port = FakeExecPort()
    strategy = make_strategy(tmp_path)
    await port.bootstrap()
    await strategy.bootstrap(port)
    rt = strategy.runtime["XAUTUSD"]

    # warmup produced a tradeable zone near 100 and a bullish bias
    assert rt.zones, f"no zones after warmup: {strategy.runtime}"
    zone = min((z for z in rt.zones if abs(z.price - (100.0 if abs(z.price - 100.0) < abs(z.price - 100.5) else 100.5))
                ), key=lambda z: z.price) if False else None
    near_100 = [z for z in rt.zones if abs(z.price - 100.0) <= 0.35]
    assert near_100
    assert rt.atr1h > 0.5
    assert rt.structure.bias == "bullish"

    # -- sweep:w ick below zone low >= 0.1 x ATR1H, close back above level
    i0 = 25  # 15m bar index after the 25 warmup bars
    base_min = i0 * 15
    sweep = _c("15m", i0, 15, o=100.9, h=101.0, l=94.5, cl=100.8, v=30.0)
    await strategy.on_candle_close(sweep)
    assert strategy.sm.state_of("XAUTUSD") in ("ARMED", "IDLE")
    # no reclaim yet -> no campaign
    assert strategy.ledger.get_campaigns(symbol="XAUTUSD") == [] or all(
        (c.get("state") or "").upper() == "CLOSED"
        for c in strategy.ledger.get_campaigns(symbol="XAUTUSD"))

    reclaim = _c("15m", i0 + 1, 15, o=100.8, h=101.2, l=100.5, cl=101.0, v=10.0)
    await strategy.on_candle_close(reclaim)
    await port.pump(strategy)

    state = strategy.ledger.get_campaigns(symbol="XAUTUSD")
    assert len(state) == 1
    cam_row = state[0]
    assert cam_row["direction"] == "long"
    cid = int(cam_row["id"])

    meta = cam_row["meta"]
    rungs = meta["rungs"]
    assert len(rungs) == 2
    r1, r2 = rungs
    e1 = r1["price"]
    assert r1["contracts"] > 0 and r2["contracts"] > 0
    # no rung beyond invalidation (sweep low 94.5 - 0.5 x ATR1H)
    assert e1 == pytest.approx(100.95, abs=1e-6)
    assert r2["price"] == pytest.approx(e1 - 0.6 * rt.atr1h, abs=1e-6)
    assert r2["price"] > float(meta["sl_price"])
    # risk cap: all rungs filled + SL -> loss <= 1% of equity
    risk = sum(rg["margin"] * 5.0 * abs(rg["price"] - float(meta["sl_price"]))
               / rg["price"] for rg in rungs)
    assert risk <= 100_000.0 * 0.01 + 1e-6

    # rung1 was a post-only limit, resting below the mark until it trades
    rung1_intents = [si for si in port.orders_sent if si.purpose == "rung1"]
    assert len(rung1_intents) == 1 and rung1_intents[0].post_only is True
    assert strategy.sm.history("XAUTUSD") == ["ARMED", "TRIGGERED"]

    # -- price edges down: rung1 fills -> BUILDING, rung2 rests
    port.set_mark(e1 - 0.1)
    await port.pump(strategy)
    meta = strategy.ledger.get_campaigns(symbol="XAUTUSD")[0]["meta"]
    assert meta["rungs"][0]["status"] == "filled"
    hist = strategy.sm.history("XAUTUSD")
    assert hist == ["ARMED", "TRIGGERED", "BUILDING"], hist
    lots = strategy.ledger.open_lots(sleeve="rwa_positional", symbol="XAUTUSD")
    assert len(lots) == 1
    assert int(lots[0]["contracts"]) == r1["contracts"]
    assert abs(float(lots[0]["entry_price"]) - e1) < 1e-9

    # -- price reaches rung2: ladder add fills
    port.set_mark(r2["price"] - 0.05)
    await port.pump(strategy)
    cam_row = strategy.ledger.get_campaigns(symbol="XAUTUSD")[0]
    meta = cam_row["meta"]
    assert all(rg["status"] == "filled" for rg in meta["rungs"])
    assert strategy.sm.state_of("XAUTUSD") == "MANAGING"
    lots = strategy.ledger.open_lots(sleeve="rwa_positional", symbol="XAUTUSD")
    assert len(lots) == 2
    # bracket re-centre happened at least twice (after each rung fill)
    brackets = [si for si in port.orders_sent if si.purpose == "bracket"]
    assert len(brackets) >= 2
    final_bracket = port.bracket
    assert final_bracket["size"] == r1["contracts"] + r2["contracts"]
    avg = (r1["contracts"] * e1 + r2["contracts"] * r2["price"]) / final_bracket["size"]
    assert final_bracket["tp"] == pytest.approx(avg * (1 + 0.20 / 5.0), rel=1e-6)
    assert final_bracket["sl"] == float(meta["sl_price"])

    # -- mark ticks to the software TP: equal price move = 0.20 / leverage
    port.set_mark(final_bracket["tp"] + 0.05)
    await port.pump(strategy)
    assert strategy.sm.state_of("XAUTUSD") in ("COOLDOWN",)
    cam_row = strategy.ledger.get_campaigns(symbol="XAUTUSD")[0]
    assert (cam_row["state"] or "").upper() == "CLOSED"
    assert cam_row["close_reason"] == "exchange_tp"
    lots = strategy.ledger.open_lots(sleeve="rwa_positional", symbol="XAUTUSD")
    assert lots == []          # whole campaign closed
    # closed lots priced at the bracket tp
    db = DeltaSchema(str(tmp_path / "rwa.db"), table_prefix="rwa_")
    closed_lots = db.query("SELECT * FROM rwa_lots WHERE status='closed'")
    assert len(closed_lots) == 2
    exits = {float(l["close_price"]) for l in closed_lots}
    assert exits == {float(final_bracket["tp"])}

    hist = strategy.sm.history("XAUTUSD")
    assert hist == ["ARMED", "TRIGGERED", "BUILDING", "MANAGING", "EXITING",
                    "COOLDOWN"], hist

    # -- events: decision rows exist
    events = strategy.events.recent(200, symbol="XAUTUSD")
    kinds = {ev["kind"] for ev in events}
    assert {"signal", "entry", "fill", "exit", "reconcile"} <= kinds

    # -- cooldown expiry after cooldown_bars 1H closes
    lastmin = 60 * 40 + 15 * (i0 + 2)
    for k in range(6):
        idx = 40 + k
        h_ = strategy.runtime["XAUTUSD"].candles["1H"][-1]
        minutes_total = (h_.ts - BASE).total_seconds() // 60 + 60
        nxt = _c("1H", 0, int(minutes_total), o=101.4, h=102.0, l=101.2, cl=101.8)
        await strategy.on_candle_close(nxt)
    assert strategy.sm.state_of("XAUTUSD") == "IDLE"


async def test_entry_timeout_cross_fallback(tmp_path):
    """rung1 post-only not filled within entry_timeout_s -> one IOC cross."""
    port = FakeExecPort()
    port.mark = 101.8          # above the rung1 bid limit -> stays resting
    strategy = make_strategy(tmp_path)
    await port.bootstrap()
    await strategy.bootstrap(port)
    rt = strategy.runtime["XAUTUSD"]
    i0 = 25
    await strategy.on_candle_close(_c("15m", i0, 15, o=100.9, h=101.0,
                                      l=94.5, cl=100.8, v=30.0))
    await strategy.on_candle_close(_c("15m", i0 + 1, 15, o=100.8, h=101.2,
                                      l=100.5, cl=101.0, v=10.0))
    await port.pump(strategy)
    assert strategy.ledger.get_campaigns(symbol="XAUTUSD")
    rung1 = [si for si in port.orders_sent if si.purpose == "rung1"]
    assert len(rung1) == 1 and rung1[0].post_only is True

    from datetime import datetime as dt, timedelta as td, timezone as tz
    strategy.runtime["XAUTUSD"].entry_placed_ts = dt.now(tz.utc) - td(seconds=30)
    await strategy.on_timer()
    r_ioc = [si for si in port.orders_sent if si.purpose == "rung1_ioc"]
    assert len(r_ioc) == 1
    # IOC crosses: buy at best_ask + 2 ticks
    assert r_ioc[0].time_in_force == TimeInForce.IOC
    assert r_ioc[0].post_only is False
    assert float(r_ioc[0].limit_price) == pytest.approx(port.mark + port.spread / 2 + 2 * 0.1, abs=0.11)
    await port.pump(strategy)
    assert strategy.sm.state_of("XAUTUSD") == "BUILDING"


async def test_counter_trend_blocked_at_signal_level(tmp_path):
    """Bearish 4H bias + sell-side sweep -> direction filter blocks (default off)."""
    port = FakeExecPort()
    strategy = make_strategy(tmp_path)
    await port.bootstrap()
    await strategy.bootstrap(port)
    rt = strategy.runtime["XAUTUSD"]
    # force bearish structure
    rt.structure.bias = "bearish"
    rt.zones = [type(rt.zones[0])(price=100.0, score=2, upper=100.5, lower=99.5)
                ] if rt.zones else []
    i0 = 25
    await strategy.on_candle_close(_c("15m", i0, 15, o=100.9, h=101.0,
                                      l=94.5, cl=100.8, v=30.0))
    await strategy.on_candle_close(_c("15m", i0 + 1, 15, o=100.8, h=101.2,
                                      l=100.5, cl=101.0, v=10.0))
    await port.pump(strategy)
    assert not [c for c in strategy.ledger.get_campaigns(symbol="XAUTUSD")
                if (c.get("state") or "").upper() != "CLOSED"]


async def test_reconcile_detects_mismatch(tmp_path):
    port = FakeExecPort()
    strategy = make_strategy(tmp_path)
    await port.bootstrap()
    await strategy.bootstrap(port)
    # inject an exchange position the ledger does not know about
    port.injected_position = PositionSnapshot(
        symbol="XAUTUSD", product_id=101, net_size=5)
    mismatches = await strategy.reconcile()
    assert mismatches and "XAUTUSD" in mismatches[0]
    assert "exchange net 5 != ledger lots 0" in mismatches[0]
