"""Live ExecutionPort: real orders against Delta Exchange India.

Implements core.ports.ExecutionPort over the delta client stack
(DeltaRestClient + ProductCatalog + DeltaOrderManager + DeltaWSClient +
DeadmanSwitch). Strategies call place()/cancel() — the om manager enforces
idempotency (never blind-retries POST, lookup by client_order_id first).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Optional

from core.config import Config
from core.models import (
    AccountSnapshot,
    Candle,
    EventNote,
    FillEvent,
    OrderIntent,
    OrderRecord,
    OrderResult,
    OrderStatus,
    PositionSnapshot,
    ProductInfo,
    Side,
    Ticker,
)
from core.ports import ExecutionPort

from delta.auth import AuthSigner
from delta.heartbeat import DeadmanSwitch
from delta.orders import DeltaOrderManager
from delta.products import ProductCatalog
from delta.rest import DeltaRestClient
from delta.ws import DeltaWSClient


def _dec(v: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(v)) if v is not None else None
    except Exception:
        return None


class DeltaPort(ExecutionPort):
    """spec 3.2: third implementation of the same contract."""

    def __init__(self, cfg: Config, ws_auth_needed: bool = True):
        self.cfg = cfg
        self.rest = DeltaRestClient(
            base_url=cfg.api.rest_base,
            api_key=cfg.api.api_key,
            api_secret=cfg.api.api_secret,
        )
        self.catalog = ProductCatalog(self.rest)
        self.order_manager = DeltaOrderManager(self.rest, self.catalog)
        self.signer = AuthSigner(api_key=cfg.api.api_key, api_secret=cfg.api.api_secret)
        self.ws = DeltaWSClient(cfg.api.ws_base, signer=self.signer, auth_needed=ws_auth_needed)
        self.deadman = DeadmanSwitch(
            self.rest,
            enabled=True,
            has_credentials=bool(cfg.api.api_key and cfg.api.api_secret),
        )
        self.pending_fills: list[FillEvent] = []
        self.margin_mode: Optional[str] = None
        self.subscribed_symbols: list[str] = []

    # ----- lifecycle --------------------------------------------------------
    async def bootstrap(self) -> None:
        await self.catalog.load()
        if self.cfg.env == "prod":
            await self.deadman.start()

    async def add_ws_subscriptions(self) -> None:
        for channel in ("candlesticks", "ticker", "mark_price", "funding_rate", "system_status"):
            self.ws.add_subscription(channel, self.subscribed_symbols)
        self.ws.add_subscription("orders", None)   # private (orders/positions/margins/trades)
        self.ws.add_subscription("positions", None)
        self.ws.add_subscription("margins", None)
        self.ws.add_subscription("user_trades", None)
        self.ws.on_reconnect_full_resync(self._resync)

    async def _resync(self) -> None:
        # after reconnect: full REST reconciliation gate before trading resumes
        snap = await self.account()
        self.pending_fills.extend(await self.withdraw_fills_from_orders(snap.positions))
        self.ws.is_reconnecting = False

    async def withdraw_fills_from_orders(self, positions: list[PositionSnapshot]) -> list[FillEvent]:
        """TODO(VERIFY) — reconcile recent fills after reconnect via /fills."""
        return []

    async def start_deadman(self, on_ack=None) -> bool:
        return await self.deadman.start(on_ack)

    async def shutdown(self) -> None:
        await self.deadman.stop()
        await self.ws.stop()
        await self.rest.aclose()

    # ----- market data -------------------------------------------------------
    async def product(self, symbol: str) -> Optional[ProductInfo]:
        return self.catalog.get(symbol)

    async def products(self) -> list[ProductInfo]:
        return self.catalog.all()

    async def ticker(self, symbol: str) -> Optional[Ticker]:
        raw = await self.rest.get_ticker(symbol)
        payload = raw if isinstance(raw, dict) else {}
        quotes = payload.get("quotes") or {}
        return Ticker(
            symbol=symbol,
            mark_price=_dec(payload.get("mark_price")),
            spot_price=_dec(payload.get("spot_price")),
            best_bid=_dec(quotes.get("best_bid")),
            best_ask=_dec(quotes.get("best_ask")),
            volume=_dec(payload.get("vol")),
            oi=_dec(payload.get("oi")),
            turnover_usd=_dec(payload.get("turnover", payload.get("vol_usd"))),
            funding_rate=_dec(payload.get("funding_rate")),
            delta=_dec((payload.get("greeks") or {}).get("delta")),
            iv=_dec(payload.get("mark_vol")),
            strike_price=_dec(payload.get("strike_price")),
            expiry_date=payload.get("expiry_date"),
            raw=payload,
        )

    async def candles(self, symbol: str, resolution: str, start: int, end: int) -> list[Candle]:
        from datetime import datetime, timezone

        rows = await self.rest.get_candles(symbol, resolution, start, end)
        out: list[Candle] = []
        for row in rows or []:
            t = row.get("time") or row.get("ts")
            ts = _candle_ts(t)
            out.append(
                Candle(
                    symbol=symbol,
                    resolution=resolution,
                    ts=ts,
                    open=float(row.get("open", 0) or 0),
                    high=float(row.get("high", 0) or 0),
                    low=float(row.get("low", 0) or 0),
                    close=float(row.get("close", 0) or 0),
                    volume=float(row.get("volume", 0) or 0),
                    closed=True,
                )
            )
        out.sort(key=lambda c: c.ts)
        return out

    # ----- account / positions ----------------------------------------------
    async def account(self) -> AccountSnapshot:
        balances = await self.rest.get_wallet_balances() or []
        wallet = Decimal("0")
        available = Decimal("0")
        for b in balances:
            try:
                if str(b.get("asset_symbol", "")).upper() in ("USDT", "USD"):
                    wallet += _dec(b.get("wallet_balance")) or Decimal("0")
                    available += _dec(b.get("available_balance")) or Decimal("0")
            except Exception:
                continue
        pos = await self.position_snapshots()
        # TODO(VERIFY): margin/utilisation fields on wallet endpoint live docs.
        equity = wallet  # live unrealized pnl lives on positions; refined below
        for p in pos:
            if p.unrealized_pnl:
                equity += p.unrealized_pnl
        used = sum((p.margin or Decimal("0") for p in pos), Decimal("0"))
        return AccountSnapshot(
            equity=equity,
            wallet_balance=wallet,
            available_balance=available,
            used_margin=used,
            positions=pos,
            raw={"balances": balances},
        )

    async def position_snapshots(self) -> list[PositionSnapshot]:
        out = []
        for p in await self.rest.get_positions_margined() or []:
            size = int(p.get("size", 0) or 0)
            side = str(p.get("side", "")).lower()
            signed = size if side in ("buy", "long") else -size
            out.append(
                PositionSnapshot(
                    symbol=str(p.get("product_symbol", p.get("symbol", ""))),
                    product_id=p.get("product_id"),
                    net_size=signed,
                    entry_price=_dec(p.get("avg_entry_price")),
                    mark_price=_dec(p.get("mark_price")),
                    liquidation_price=_dec(p.get("liquidation_price")),
                    unrealized_pnl=_dec(p.get("pnl")),
                    margin=_dec(p.get("margin")),
                    effective_leverage=_dec(p.get("leverage")),
                )
            )
        return out

    async def position(self, symbol: str) -> Optional[PositionSnapshot]:
        for p in await self.position_snapshots():
            if p.symbol == symbol:
                return p
        return None

    # ----- orders -------------------------------------------------------------
    async def open_orders(self, symbol: Optional[str] = None) -> list[OrderRecord]:
        ids = None
        if symbol:
            p = self.catalog.get(symbol)
            ids = str(p.product_id) if p else None
        raw = await self.rest.get_orders(states="open,pending", product_ids=ids) or []
        out = []
        for o in raw:
            out.append(
                OrderRecord(
                    client_order_id=str(o.get("client_order_id") or ""),
                    symbol=str(o.get("product_symbol", symbol or "")),
                    side=Side(str(o.get("side")).lower()) if o.get("side") in ("buy", "sell") else Side.BUY,
                    size=int(o.get("size", 0) or 0),
                    filled_size=int(o.get("filled_size", 0) or 0),
                    avg_fill_price=_dec(o.get("average_fill_price")),
                    status=OrderStatus.OPEN,
                    order_type=str(o.get("order_type", "")),
                    limit_price=_dec(o.get("limit_price")),
                    reduce_only=bool(o.get("reduce_only", False)),
                    product_id=o.get("product_id"),
                    exchange_order_id=str(o.get("id")) if o.get("id") is not None else None,
                )
            )
        return out

    async def place(self, intent: OrderIntent) -> OrderResult:
        if not intent.client_order_id:
            raise ValueError("OrderIntent needs client_order_id (idempotency, spec 1.7)")
        return await self.order_manager.place_order(intent)

    async def cancel(self, client_order_id: str, symbol: str) -> bool:
        p = self.catalog.get(symbol)
        if not p:
            return False
        return await self.order_manager.cancel_by_client_id(client_order_id, p.product_id)

    async def reconcile(self, ledger_state: dict[str, Any]) -> list[str]:
        """sum of open lots (signed) per symbol == exchange net position."""
        mismatches = []
        positions = {p.symbol: p.net_size for p in await self.position_snapshots()}
        for sym, expected in ledger_state.items():
            actual = positions.get(sym)
            if actual is None:
                if int(expected) != 0:
                    mismatches.append(f"{sym}: ledger {expected} vs exchange 0")
            elif int(expected) != actual:
                mismatches.append(f"{sym}: ledger {expected} vs exchange {actual}")
        return mismatches

    def note(self, note: EventNote) -> None:
        from core.events import EventLogger  # imported lazily to avoid cycles

        getattr(self, "_event_logger", None) or setattr(self, "_event_logger", EventLogger("state/fm.sqlite"))
        self._event_logger.note(note)  # type: ignore[attr-defined]


def _candle_ts(t: Any):
    if t is None:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc)
    t = float(t)
    if t > 1e12:  # microseconds in responses (spec 1.3)
        t /= 1e6
    from datetime import datetime, timezone

    return datetime.fromtimestamp(t, tz=timezone.utc)
