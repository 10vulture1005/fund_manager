"""Shared data models for the fund manager.

All strategies, execution ports and the backtester build on these types.
Big decimals arrive from Delta as STRINGS; we parse into Decimal and render
back to strings when sending. `size` is an integer number of contracts.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_client_order_id(algo: str, symbol: str, purpose: str) -> str:
    """client_order_id shaped `{algo}-{symbol}-{purpose}-{uuid8}` (spec 1.7)."""
    clean = lambda s: s.replace("_", "").replace("-", "").lower()
    return f"{clean(algo)}-{clean(symbol).replace('usd', '')}-{clean(purpose)}-{uuid.uuid4().hex[:8]}"[:64]


class Side(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"

    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, enum.Enum):
    LIMIT = "limit_order"
    MARKET = "market_order"


class TimeInForce(str, enum.Enum):
    GTC = "gtc"
    IOC = "ioc"


class StrEnum(str, enum.Enum):
    pass


class ProductInfo(BaseModel):
    """Resolved from GET /products (spec 1.6). Never hard-code ids."""

    symbol: str
    product_id: int
    contract_type: str  # perpetual futures | futures | call_options | put_options | interest_rate_swap
    tick_size: Decimal
    contract_value: Decimal
    contract_unit_currency: str = ""
    default_leverage: Optional[Decimal] = None
    max_leverage_notional: Optional[Decimal] = None
    position_size_limit: Optional[int] = None
    maker_commission_rate: Decimal = Decimal("0")
    taker_commission_rate: Decimal = Decimal("0")
    trading_status: str = "operational"
    only_reduce_only_orders_allowed: bool = False
    tags: list[str] = Field(default_factory=list)
    # price band: [low, high]; None when the API does not return one
    price_band_low: Optional[Decimal] = None
    price_band_high: Optional[Decimal] = None
    # option-only fields
    strike_price: Optional[Decimal] = None
    expiry_date: Optional[str] = None
    underlying_asset_symbol: Optional[str] = None
    description: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_option(self) -> bool:
        return self.contract_type in ("call_options", "put_options")

    @property
    def is_perp(self) -> bool:
        return self.contract_type == "perpetual_futures"


class Candle(BaseModel):
    symbol: str
    resolution: str  # "1m", "15m", "1H", "4H", "1D"
    ts: datetime  # open time (UTC, tz-aware)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    closed: bool = True


class OrderIntent(BaseModel):
    """What a strategy wants the ExecutionPort to do. Also printed dry-run (spec 3.4)."""

    algo: str
    product_symbol: str
    product_id: Optional[int] = None
    side: Side
    size: int  # contracts; must be > 0
    order_type: OrderType = OrderType.LIMIT
    limit_price: Optional[Decimal] = None
    stop_price: Optional[Decimal] = None
    stop_order_type: Optional[str] = None  # stop_loss_order | take_profit_order
    stop_trigger_method: Optional[str] = None  # mark_price | last_traded_price | spot_price
    time_in_force: TimeInForce = TimeInForce.GTC
    post_only: bool = False
    reduce_only: bool = False
    client_order_id: str = ""
    bracket_stop_loss_price: Optional[Decimal] = None
    bracket_take_profit_price: Optional[Decimal] = None
    bracket_trail_amount: Optional[Decimal] = None
    purpose: str = ""  # e.g. rung1, rung_add, lot_tp, flat
    reason: str = ""  # human-readable "why" for logs / events
    meta: dict[str, Any] = Field(default_factory=dict)

    def with_client_id(self, algo: str, symbol: str, purpose: str) -> "OrderIntent":
        if not self.client_order_id:
            self.client_order_id = new_client_order_id(algo, symbol, purpose)
        return self


class OrderStatus(str, enum.Enum):
    NEW = "new"            # recorded, not yet confirmed by exchange
    OPEN = "open"
    FILLED = "filled"
    PARTIAL = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class OrderRecord(BaseModel):
    client_order_id: str
    symbol: str
    side: Side
    size: int
    filled_size: int = 0
    avg_fill_price: Optional[Decimal] = None
    status: OrderStatus = OrderStatus.NEW
    order_type: str = ""
    limit_price: Optional[Decimal] = None
    reduce_only: bool = False
    product_id: Optional[int] = None
    exchange_order_id: Optional[str] = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)


class FillEvent(BaseModel):
    client_order_id: str
    symbol: str
    product_id: Optional[int] = None
    side: Side
    size: int
    price: Decimal
    ts: datetime = Field(default_factory=now_utc)
    fee: Decimal = Decimal("0")
    reduce_only: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)


class PositionSnapshot(BaseModel):
    symbol: str
    product_id: Optional[int]
    net_size: int  # signed contracts
    entry_price: Optional[Decimal] = None
    mark_price: Optional[Decimal] = None
    liquidation_price: Optional[Decimal] = None
    unrealized_pnl: Optional[Decimal] = None
    margin: Optional[Decimal] = None
    effective_leverage: Optional[Decimal] = None


class AccountSnapshot(BaseModel):
    equity: Decimal         # wallet balance + unrealized pnl
    wallet_balance: Decimal = Decimal("0")
    available_balance: Decimal = Decimal("0")
    used_margin: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    positions: list[PositionSnapshot] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class OrderResult(BaseModel):
    client_order_id: str
    accepted: bool
    order_id: Optional[str] = None
    status: OrderStatus = OrderStatus.NEW
    filled_size: int = 0
    avg_fill_price: Optional[Decimal] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Ticker(BaseModel):
    symbol: str
    mark_price: Optional[Decimal] = None
    spot_price: Optional[Decimal] = None
    best_bid: Optional[Decimal] = None
    best_ask: Optional[Decimal] = None
    volume: Optional[Decimal] = None
    oi: Optional[Decimal] = None
    turnover_usd: Optional[Decimal] = None
    funding_rate: Optional[Decimal] = None
    delta: Optional[Decimal] = None  # options greeks.delta
    iv: Optional[Decimal] = None
    strike_price: Optional[Decimal] = None
    expiry_date: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def spread_pct(self) -> Optional[Decimal]:
        if self.best_bid and self.best_ask and self.best_bid > 0:
            mid = (self.best_bid + self.best_ask) / 2
            if mid > 0:
                return (self.best_ask - self.best_bid) / mid
        return None


class Event(str, enum.Enum):
    SIGNAL = "signal"
    ENTRY = "entry"
    EXIT = "exit"
    SKIP = "skip"
    RISK_VETO = "risk_veto"
    HALT = "halt"
    RECONCILE = "reconcile"
    RECONNECT = "reconnect"
    FILL = "fill"
    KILL = "kill"
    INFO = "info"


class EventNote(BaseModel):
    """One row in the shared `events` table (spec 3.5): decision + feature snapshot."""

    ts: datetime = Field(default_factory=now_utc)
    algo: str
    kind: Event
    symbol: Optional[str] = None
    message: str
    features: dict[str, Any] = Field(default_factory=dict)
