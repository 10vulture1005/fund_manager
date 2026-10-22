"""Paper ExecutionPort (spec 3.2): real market data, simulated fills, and
absolutely zero orders sent.

- Candles/tickers come from the live Delta India REST (public, no auth needed).
- place() crosses against the current best bid/ask when possible and simulates
  an immediate fill (paper is intentional: instant, symmetric, no queueing).
- Fees are charged at the product's taker rate; no funding accrual.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
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
    OrderType,
    PositionSnapshot,
    ProductInfo,
    Side,
    Ticker,
    now_utc,
)
from core.ports import ExecutionPort

from delta.products import ProductCatalog
from delta.rest import BASE_URLS, DeltaRestClient

log = logging.getLogger("fm.paper")


def _dec(v: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(v)) if v is not None else None
    except Exception:
        return None


class PaperPort(ExecutionPort):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.rest = DeltaRestClient(
            base_url=cfg.api.rest_base,
            api_key="",  # public market data only
            api_secret="",
        )
        self.catalog = ProductCatalog(self.rest)
        self.pending_fills: list[FillEvent] = []
        self.placed_orders: list[dict[str, Any]] = []  # dry-run audit trail
        # simulated account
        self.equity_sim = Decimal(str(cfg.flags.get("paper_initial_equity_usd", 100000)))
        self.positions: dict[str, dict[str, Any]] = {}  # symbol -> {size, entry, mark}
        # optional persistence so paper sessions (e.g. GitHub Actions) resume
        # the simulated account instead of resetting it every run (spec 3.2)
        self.state_path = cfg.flags.get("paper_state_path")
        self._restore()

    async def bootstrap(self) -> None:
        await self.catalog.load()
        self.save_state()

    # ----------------------------------------------------- persistence
    def _restore(self) -> None:
        """Reload simulated equity + open positions from the last session.

        A corrupted state file is discarded loudly and the account restarts
        fresh: paper truth must never be quietly fudged.
        """
        if not self.state_path:
            return
        path = Path(self.state_path)
        try:
            data = json.loads(path.read_text())
            self.equity_sim = Decimal(str(data.get("equity", self.equity_sim)))
            self.positions = {
                sym: {
                    "size": int(p["size"]),
                    "entry": Decimal(str(p["entry"])),
                    "mark": _dec(p.get("mark")) or Decimal(str(p["entry"])),
                }
                for sym, p in (data.get("positions") or {}).items()
            }
            log.info("paper account restored from %s (equity=%s, %d positions)",
                     path, self.equity_sim, len(self.positions))
        except FileNotFoundError:
            return
        except Exception:
            log.warning("paper state %s unusable — starting a fresh account", path)

    def save_state(self) -> None:
        """Atomically dump the simulated account (equity + positions)."""
        if not self.state_path:
            return
        try:
            path = Path(self.state_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "saved_at": now_utc().isoformat(),
                "equity": str(self.equity_sim),
                "positions": {
                    sym: {
                        "size": int(p["size"]),
                        "entry": str(p["entry"]),
                        "mark": str(p.get("mark") or p["entry"]),
                    }
                    for sym, p in self.positions.items()
                },
            }
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(path)
        except Exception:
            log.exception("paper state save failed (%s)", self.state_path)

    def _contract_value(self, symbol: str) -> Decimal:
        prod = self.catalog.get(symbol)
        return prod.contract_value if prod else Decimal("1")

    def unrealized_pnl(self) -> Decimal:
        """Mark-to-market PnL over currently open positions (mark vs entry)."""
        pnl = Decimal("0")
        for sym, p in self.positions.items():
            size, entry, mark = int(p["size"]), p["entry"], p.get("mark")
            if not size or mark is None:
                continue
            pnl += Decimal(size) * (mark - entry) * self._contract_value(sym)
        return pnl

    def mark_to_market_equity(self) -> Decimal:
        """Cash equity adjusted for unrealized PnL of open positions."""
        return self.equity_sim + self.unrealized_pnl()

    # ----- market data (all real) ---------------------------------------------
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
            delta=_dec((payload.get("greeks") or {}).get("delta")),
            raw=payload,
        )

    async def candles(self, symbol: str, resolution: str, start: int, end: int) -> list[Candle]:
        rows = await self.rest.get_candles(symbol, resolution, start, end)
        out: list[Candle] = []
        from datetime import datetime, timezone

        for row in rows or []:
            t = float(row.get("time") or 0)
            t = t / 1e6 if t > 1e12 else t
            out.append(
                Candle(
                    symbol=symbol,
                    resolution=resolution,
                    ts=datetime.fromtimestamp(t, tz=timezone.utc),
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

    # ----- simulated account -----------------------------------------------------
    async def account(self) -> AccountSnapshot:
        positions = []
        for sym, p in self.positions.items():
            if p["size"] == 0:
                continue
            prod = self.catalog.get(sym)
            mark = p.get("mark")
            notional = abs(Decimal(p["size"])) * (mark or p["entry"]) * (prod.contract_value if prod else Decimal("1"))
            lev = prod.default_leverage or Decimal("10") if prod else Decimal("10")
            positions.append(
                PositionSnapshot(
                    symbol=sym,
                    product_id=prod.product_id if prod else None,
                    net_size=int(p["size"]),
                    entry_price=p["entry"],
                    mark_price=mark,
                    margin=notional / lev,
                )
            )
        return AccountSnapshot(equity=self.equity_sim, positions=positions, used_margin=sum(
            (p.margin or Decimal("0") for p in positions), Decimal("0")
        ))

    async def position(self, symbol: str) -> Optional[PositionSnapshot]:
        return next((p for p in (await self.account()).positions if p.symbol == symbol), None)

    async def open_orders(self, symbol: Optional[str] = None) -> list[OrderRecord]:
        return []  # paper fills instantly; nothing rests

    async def place(self, intent: OrderIntent) -> OrderResult:
        """Simulate a fill at the best bid/ask/mark. Zero exchange calls."""
        tick = (self.catalog.get(intent.product_symbol).tick_size
                if self.catalog.get(intent.product_symbol) else Decimal("0.1"))
        cv = self.catalog.get(intent.product_symbol).contract_value if self.catalog.get(intent.product_symbol) else Decimal("1")
        t = await self.ticker(intent.product_symbol)
        mark = None
        if t:
            if t.mark_price:
                mark = t.mark_price
            elif t.best_bid and t.best_ask:
                mark = (t.best_bid + t.best_ask) / 2
        if mark is None:
            return OrderResult(
                client_order_id=intent.client_order_id,
                accepted=False,
                status=OrderStatus.REJECTED,
                error_code="no_mark",
                error_message="paper port has no market data for symbol",
            )
        if intent.reduce_only:
            px = t.best_bid if intent.side is Side.SELL else t.best_ask
            px = px or mark
        else:
            px = t.best_ask if intent.side is Side.BUY else t.best_bid
            px = px or mark
        notional = Decimal(intent.size) * px * cv
        prod = self.catalog.get(intent.product_symbol)
        fee = notional * (prod.taker_commission_rate if prod else Decimal("0.0007"))
        signed = intent.size if intent.side is Side.BUY else -intent.size
        cur = self.positions.setdefault(intent.product_symbol, {"size": 0, "entry": px, "mark": mark})
        cur["size"] += int(signed)
        cur["mark"] = mark
        self.equity_sim -= fee
        fill = FillEvent(
            client_order_id=intent.client_order_id,
            symbol=intent.product_symbol,
            side=intent.side,
            size=intent.size,
            price=px,
            ts=now_utc(),
            fee=fee,
            reduce_only=intent.reduce_only,
        )
        self.pending_fills.append(fill)
        self.placed_orders.append(
            {
                "ts": now_utc().isoformat(),
                "intent": intent.model_dump(mode="json"),
                "fill_price": str(px),
                "fee": str(fee),
            }
        )
        self.save_state()
        return OrderResult(
            client_order_id=intent.client_order_id,
            accepted=True,
            status=OrderStatus.FILLED,
            filled_size=intent.size,
            avg_fill_price=px,
            raw={"paper": True},
        )

    async def cancel(self, client_order_id: str, symbol: str) -> bool:
        return True  # nothing ever rests in paper

    async def reconcile(self, ledger_state: dict[str, Any]) -> list[str]:
        mismatches = []
        for sym, expected in ledger_state.items():
            actual = self.positions.get(sym, {}).get("size", 0)
            if int(expected) != int(actual):
                mismatches.append(f"{sym}: ledger {expected} vs paper {actual}")
        return mismatches

    def note(self, note: EventNote) -> None:
        pass

    async def aclose(self) -> None:
        await self.rest.aclose()
