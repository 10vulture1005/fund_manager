"""Fill simulation rules (prompt spec 3.6, both bots share these).

Conservative by design:
- A resting limit fills only if price trades THROUGH it by at least one tick.
  (Touch-only fills are configurable but default OFF.)
- When SL and TP are both inside one candle, assume SL first.
- Market orders fill at next-bar open ± slippage (config bps).
No lookahead: every rule sees only the closed candle being processed.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Optional

from core.models import Candle, OrderRecord, Side


@dataclass
class FillResult:
    filled: bool = False
    fill_price: Optional[Decimal] = None
    quantity: int = 0
    reason: str = ""


@dataclass
class FillSimulator:
    """"""

    tick_sizes: dict[str, Decimal] = field(default_factory=dict)
    slippage_bps: Decimal = Decimal("1")           # market-order slippage
    touch_fill_prob: float = 0.0                   # OFF by default (spec 3.6)
    assume_sl_first: bool = True                   # SL before TP within one candle

    def limit_fill(
        self,
        order: OrderRecord,
        candle: Candle,
        tick: Optional[Decimal] = None,
    ) -> FillResult:
        """Does a resting LIMIT order fill inside this CLOSED candle?

        Rules: fills when the candle range trades through the limit by >= 1 tick;
        with `touch_fill_prob` > 0, a mere touch fills with that probability.
        """
        if order.limit_price is None:
            return FillResult(False, reason="no limit price")
        tick = tick or self.tick_sizes.get(order.symbol, Decimal("0"))
        px = Decimal(str(order.limit_price))
        lo, hi = Decimal(str(candle.low)), Decimal(str(candle.high))

        if order.side is Side.BUY:
            hit = lo <= px
            through = px - lo >= tick if tick > 0 else lo < px
        else:
            hit = hi >= px
            through = hi - px >= tick if tick > 0 else hi > px

        if hit and (
            through
            or (self.touch_fill_prob > 0 and random.random() < self.touch_fill_prob)
        ):
            # worst realistic price: the limit price itself (post_only rests there)
            return FillResult(True, px, abs(order.size - order.filled_size), "limit")
        return FillResult(False, reason="not reached")

    def stop_market_trigger(
        self, order: OrderRecord, candle: Candle
    ) -> bool:
        """Trigger check for stop_loss_order / take_profit_order on a closed candle."""
        if order.limit_price is None:  # stop price rides in limit_price slot
            return False
        px = Decimal(str(order.limit_price))
        lo, hi = Decimal(str(candle.low)), Decimal(str(candle.high))
        if order.side is Side.SELL:  # stop below market
            return lo <= px
        return hi >= px  # BUY stop above market

    def market_fill_price(
        self,
        side: Side,
        ref_price: Decimal,
        tick: Optional[Decimal] = None,
    ) -> Decimal:
        """Market order fills at ref ± slippage (bps), rounded to tick."""
        slip = ref_price * self.slippage_bps / Decimal("10000")
        px = ref_price + slip if side is Side.BUY else ref_price - slip
        tick = tick or self.tick_sizes.get("DEFAULT", Decimal("0"))
        if tick > 0:
            rounding = ROUND_UP if side is Side.BUY else ROUND_DOWN
            px = (px / tick).to_integral_value(rounding=rounding) * tick
        return px
