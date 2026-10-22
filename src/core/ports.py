"""Execution and strategy ports.

Strategies NEVER talk to the exchange directly. They see an `ExecutionPort`
so the identical code runs in backtest / paper / live (spec 3.2).

Implementations:
- `backtest.engine.BacktestPort`   (simulated fills on candles)
- `paper.port.PaperPort`           (real market data, simulated fills, no orders)
- `live.port.DeltaPort`            (real REST/WS against Delta India)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from core.models import (
    AccountSnapshot,
    Candle,
    EventNote,
    FillEvent,
    OrderIntent,
    OrderRecord,
    OrderResult,
    PositionSnapshot,
    ProductInfo,
    Ticker,
)


class ExecutionPort(ABC):
    """Everything a strategy is allowed to do to the 'market'."""

    wired: bool = False

    @abstractmethod
    async def bootstrap(self) -> None:
        """Resolve products, load balances. Called once before strategy.bootstrap."""

    @abstractmethod
    async def product(self, symbol: str) -> Optional[ProductInfo]:
        """Cached ProductInfo; None when the symbol is not live on this venue."""

    @abstractmethod
    async def products(self) -> list[ProductInfo]:
        """All live products (already discovered via GET /products, paginated)."""

    @abstractmethod
    async def ticker(self, symbol: str) -> Optional[Ticker]:
        """Best bid/ask, mark, etc. for spread / liquidity filters."""

    @abstractmethod
    async def candles(
        self, symbol: str, resolution: str, start: int, end: int
    ) -> list[Candle]:
        """Historical OHLC (start/end seconds). Used for warmup and backtest data."""

    @abstractmethod
    async def account(self) -> AccountSnapshot:
        """Equity, margin, positions."""

    @abstractmethod
    async def position(self, symbol: str) -> Optional[PositionSnapshot]:
        """Net exchange position for this product (None when flat/absent)."""

    @abstractmethod
    async def open_orders(self, symbol: Optional[str] = None) -> list[OrderRecord]:
        """Resting orders, ours only."""

    @abstractmethod
    async def place(self, intent: OrderIntent) -> OrderResult:
        """Submit one order for real. Idempotent on client_order_id."""

    @abstractmethod
    async def cancel(self, client_order_id: str, symbol: str) -> bool:
        """Cancel a resting order. Returns True when gone (or was already gone)."""

    @abstractmethod
    async def reconcile(self, ledger_state: dict[str, Any]) -> list[str]:
        """Compare exchange positions with ledger state; return mismatch strings."""

    # ledger is provided by the host, not the strategy
    def note(self, note: EventNote) -> None:
        """Hook for structured event logging; default is a no-op."""


class StrategyPort(ABC):
    """Contract every strategy in this repo implements."""

    name: str = "base"
    algo: str = "fm"  # used in client_order_id prefixes

    @abstractmethod
    async def bootstrap(self, exec_port: ExecutionPort) -> None:
        """Warm-up: resolve products, load candles, rebuild state from DB."""

    @abstractmethod
    async def on_candle_close(self, candle: Candle) -> None:
        """Called for every CLOSED candle of every subscribed resolution."""

    @abstractmethod
    async def on_ticker(self, symbol: str, ticker: Ticker) -> None:
        """Called on ticker / mark-price ticks."""

    @abstractmethod
    async def on_fill(self, fill: FillEvent) -> None:
        """Called for our fills (own strategies AND theirs, e.g. the exchange TP)."""

    @abstractmethod
    async def on_timer(self) -> None:
        """Called on the housekeeping cadence (1-minute loop): backups, capital checks."""

    @abstractmethod
    def status(self) -> dict[str, Any]:
        """Sleeve/campaign/allocation state for the report CLI."""
