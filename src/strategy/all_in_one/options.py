"""Option-leg machinery (spec 2.5) for ETH_CC / BTC_CP / GOLD.

COVERED CALL  = long underlying perp lots + SHORT call options covering them.
COVERED PUT   = short underlying perp lots + SHORT put options covering them.

Instruments are discovered (never hard-coded) via the ExecutionPort: option
products appear in `products()` filtered by contract_type + underlying_asset_symbol;
greeks / bid / ask / OI come from `ticker(symbol)` (Ticker.delta, Ticker.iv, ...).

Coverage rule (hard, asserted at every order boundary):
    raw_exposure = |sum of open lot contracts| * perp.contract_value   [coin exposure]
    short option contracts = floor(raw_exposure * coverage_ratio / option.contract_value)
    and short_contracts * option.contract_value <= raw_exposure   (what makes it covered)

Roll/close rules:
- buy back when >= opt_buyback_capture_pct of premium decayed
- DTE <= 1: let it expire (assume cash settlement - TODO(VERIFY) Delta docs),
  sell the next cycle's option afterwards
- |delta| > opt_roll_delta: roll only for a net credit (new mid < open premium)
- limit orders only, start at mid, reprice toward the touch every opt_reprice_s
  up to opt_max_reprices, then give up (retry next cycle); never market.

NO options for RWA (XAUT/PAXG) -> hedge_mode none (perp ladder + LIFO only).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from core.ports import ExecutionPort
from strategy.all_in_one.defaults import get


# ---------------------------------------------------------------- helpers
def parse_expiry(expiry: Optional[str]) -> Optional[datetime]:
    """Delta expiry formats: DD-MM-YYYY (tickers) or ddmmyy (option symbols)."""
    if not expiry:
        return None
    e = str(expiry).strip()
    for fmt, ln in (("%d-%m-%Y", 10), ("%d%m%y", 6), ("%d%m%Y", 8), ("%Y-%m-%d", 10)):
        try:
            if len(e) == ln:
                return datetime.strptime(e, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def dte_days(expiry: Optional[str], now: datetime) -> Optional[float]:
    exp = parse_expiry(expiry)
    if exp is None:
        return None
    return (exp - now).total_seconds() / 86400.0


# -------------------------------------------------------------- selection
@dataclass
class OptionCandidate:
    symbol: str
    product_id: Optional[int]
    kind: str                                  # "call" | "put"
    underlying: str
    dte: float
    strike: Decimal
    delta: Optional[Decimal]
    iv: Optional[Decimal] = None
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    oi: Optional[Decimal] = None
    contract_value: Decimal = Decimal("1")
    expiry_date: Optional[str] = None

    @property
    def mid(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None and self.bid > 0:
            return float((self.bid + self.ask) / 2)
        return None

    @property
    def spread_pct(self) -> Optional[float]:
        if self.bid is not None and self.ask is not None and self.bid > 0:
            mid = (self.bid + self.ask) / 2
            if mid > 0:
                return float((self.ask - self.bid) / mid)
        return None


def select_option(
    candidates: list[OptionCandidate],
    kind: str,
    target_delta: float,
    delta_lo: float,
    delta_hi: float,
    max_spread_pct: float,
    min_oi: float,
    now: datetime,
    dte_min: float,
    dte_max: float,
) -> Optional[OptionCandidate]:
    """Expiry inside the DTE window, then the strike whose |delta| is nearest
    the target within [delta_lo, delta_hi]; rejects wrong kind / wrong DTE /
    out-of-band delta / no positive bid / too-wide spread / low OI."""
    ok: list[OptionCandidate] = []
    for c in candidates:
        if c.kind != kind:
            continue
        if c.dte is None or not (dte_min <= c.dte <= dte_max):
            continue
        if c.delta is None:
            continue
        ad = abs(float(c.delta))
        if not (delta_lo <= ad <= delta_hi):
            continue
        if not c.bid or c.bid <= 0:
            continue
        sp = c.spread_pct
        if sp is None or sp > max_spread_pct:
            continue
        if c.oi is not None and float(c.oi) < min_oi:
            continue
        ok.append(c)
    if not ok:
        return None
    return min(ok, key=lambda c: abs(abs(float(c.delta)) - target_delta))


def coverage_contracts(
    coin_exposure: float | Decimal,
    coverage_ratio: float,
    option_contract_value: float | Decimal,
) -> int:
    """floor(exposure * coverage_ratio / cv), hard-capped so the short option
    side NEVER exceeds the underlying exposure (asserted at every boundary)."""
    exp = float(coin_exposure)
    cv = float(option_contract_value)
    if exp <= 0 or cv <= 0:
        return 0
    raw = math.floor(exp * coverage_ratio / cv + 1e-9)
    hard_cap = math.floor(exp / cv + 1e-9)
    return max(0, min(raw, hard_cap))


# ------------------------------------------------------------- live chain
class OptionChain:
    """Fetches live option candidates for an underlying via the ExecutionPort."""

    def __init__(self, exec_port: ExecutionPort, cfg: dict) -> None:
        self.exec_port = exec_port
        self.cfg = cfg
        self._cache: dict[str, tuple[datetime, list[OptionCandidate]]] = {}
        self.ttl = timedelta(minutes=5)

    async def fetch(self, underlying: str, now: datetime) -> list[OptionCandidate]:
        """All live call/put candidates for `underlying` (e.g. 'ETH', 'XAUT')."""
        cached = self._cache.get(underlying)
        if cached and (now - cached[0]) < self.ttl:
            return cached[1]
        want = underlying.upper()
        out: list[OptionCandidate] = []
        try:
            products = await self.exec_port.products()
        except Exception:
            products = []
        for p in products:
            if not p.is_option:
                continue
            if (p.underlying_asset_symbol or "").upper() != want:
                continue
            t = await self.exec_port.ticker(p.symbol)
            dt = dte_days(p.expiry_date or (t.expiry_date if t else None), now)
            if dt is None:
                continue
            out.append(
                OptionCandidate(
                    symbol=p.symbol,
                    product_id=p.product_id,
                    kind="call" if p.contract_type == "call_options" else "put",
                    underlying=want,
                    dte=dt,
                    strike=p.strike_price or Decimal("0"),
                    delta=(t.delta if t else None),
                    iv=(t.iv if t else None),
                    bid=(t.best_bid if t else None),
                    ask=(t.best_ask if t else None),
                    oi=(t.oi if t else None),
                    contract_value=p.contract_value,
                    expiry_date=p.expiry_date,
                )
            )
        self._cache[underlying] = (now, out)
        return out

    def selection_params(self, cfg: dict | None = None) -> dict[str, float]:
        cfg = cfg or self.cfg
        lo, hi = (get(cfg, "opt_delta_band", [0.12, 0.30]) or [0.12, 0.30])
        return {
            "target_delta": float(get(cfg, "opt_target_delta", 0.20)),
            "delta_lo": float(lo),
            "delta_hi": float(hi),
            "max_spread_pct": float(get(cfg, "opt_max_spread_pct", 0.08)),
            "min_oi": float(get(cfg, "opt_min_oi", 100.0)),
            "dte_min": float(get(cfg, "opt_min_dte", 5)),
            "dte_max": float(get(cfg, "opt_max_dte", 10)),
        }

    async def pick(
        self,
        underlying: str,
        kind: str,
        now: datetime,
        cfg: dict | None = None,
        candidates: Optional[list[OptionCandidate]] = None,
    ) -> Optional[OptionCandidate]:
        cands = candidates if candidates is not None else await self.fetch(underlying, now)
        return select_option(
            cands, kind=kind, now=now, **self.selection_params(cfg or self.cfg),  # type: ignore[arg-type]
        )

    async def options_exist(self, underlying: str, now: datetime) -> bool:
        cands = await self.fetch(underlying, now)
        return bool([c for c in cands if c.kind in ("call", "put")])


# ------------------------------------------------------------ hedge state
@dataclass
class HedgeState:
    underlying: str                    # perp symbol the hedge covers (e.g. ETHUSD)
    kind: Optional[str] = None         # "call" (CC) | "put" (CP) | None = no hedge
    option_symbol: Optional[str] = None
    contracts: int = 0                 # short contracts on the book
    avg_entry_premium: float = 0.0
    pending_oid: Optional[str] = None  # resting hedge order (sell or buy back)
    pending_side: Optional[str] = None
    reprice_count: int = 0
    last_reprice: Optional[datetime] = None
    expiry: Optional[datetime] = None
    hedge_row_id: Optional[int] = None


class OptionHedgeManager:
    """Sleeve-level option position manager (NOT per lot), spec 2.5.

    Pass `cfg` = a flat dict with the option keys, e.g.
    {opt_*..., coverage_ratio, ...} per sleeve.
    """

    def __init__(
        self,
        exec_port: ExecutionPort,
        cfg: dict,
        ledger: Any,
        on_event: Callable[..., Any],
        algo: str = "aio",
    ) -> None:
        self.exec_port = exec_port
        self.cfg = cfg
        self.ledger = ledger
        self.on_event = on_event
        self.algo = algo
        self.state: dict[str, HedgeState] = {}

    def state_for(self, underlying: str) -> HedgeState:
        return self.state.setdefault(underlying, HedgeState(underlying=underlying))

    @property
    def coverage_ratio(self) -> float:
        return float(get(self.cfg, "coverage_ratio", 1.0))

    # ------------------------------------------------------------- lifecycle
    async def ensure(
        self,
        underlying: str,
        hedge_kind: Optional[str],
        coin_exposure: float,
        option_contract_value: float,
        chain: OptionChain,
        now: datetime,
        blocked: bool = False,
    ) -> None:
        """Re-target coverage at every lot open/close and options cycle."""
        st = self.state_for(underlying)
        # expiring leg -> stop tracking, sell next cycle (cash settlement)
        if st.expiry is not None:
            dte = (st.expiry - now).total_seconds() / 86400.0
            if dte <= 1.0:
                self.on_event(  # let it expire (assumed cash settlement, TODO(VERIFY))
                    "option_expire_wait", underlying=underlying,
                    symbol=st.option_symbol, features={"dte": dte},
                )
                return  # do not sell the (nearly) expired leg; next cycle sells anew

        target = (
            coverage_contracts(coin_exposure, self.coverage_ratio, option_contract_value)
            if hedge_kind in ("call", "put") else 0
        )
        if st.contracts > target:
            surplus = st.contracts - target
            await self._buy_back(st, surplus, now, reason="coverage_target")
        elif st.contracts < target:
            if blocked or hedge_kind is None or st.pending_oid or st.pending_side:
                return
            await self._sell(st, hedge_kind, target - st.contracts, chain, now)

    # ------------------------------------------------------------ helpers
    async def cycle(self, underlying: str, chain: OptionChain, now: datetime) -> None:
        """Options cycle (every 15 min): decay buyback, delta roll, repricing."""
        st = self.state_for(underlying)
        t = await self.exec_port.ticker(st.option_symbol) if st.option_symbol else None
        if st.option_symbol and t:
            mid = _premium(t)
            if (
                mid is not None
                and st.avg_entry_premium > 0
                and not st.pending_oid
                and mid <= st.avg_entry_premium * (1.0 - float(get(self.cfg, "opt_buyback_capture_pct", 80)) / 100.0)
            ):
                self.on_event("option_buyback_signal", underlying=underlying,
                              symbol=st.option_symbol,
                              features={"mid": mid, "entry": st.avg_entry_premium})
                await self._buy_back(st, st.contracts, now, reason="premium_decayed")
                return
            d = abs(float(t.delta)) if t.delta is not None else 0.0
            if (
                d > float(get(self.cfg, "opt_roll_delta", 0.45))
                and not st.pending_oid and st.kind
            ):
                cand = await chain.pick(underlying, st.kind, now)
                if cand and cand.mid is not None and cand.mid < st.avg_entry_premium:
                    self.on_event("option_roll", underlying=underlying,
                                  symbol=st.option_symbol, features={"delta": d,
                                  "new_mid": cand.mid, "entry": st.avg_entry_premium})
                    await self._buy_back(st, st.contracts, now, reason="roll_net_credit")
                    return   # new short leg sold on the next ensure/cycle
                else:
                    self.on_event("option_roll_skip", underlying=underlying,
                                  features={"delta": d, "reason": "no_net_credit"})
        # repricing of a resting sell that has not filled
        if st.pending_oid and st.pending_side == "sell" and st.option_symbol and t:
            every = int(get(self.cfg, "opt_reprice_s", 10))
            can = st.last_reprice is None or (
                (now - st.last_reprice).total_seconds() >= every
            )
            if can and st.reprice_count < int(get(self.cfg, "opt_max_reprices", 6)):
                st.reprice_count += 1
                st.last_reprice = now
                self.on_event("option_reprice", underlying=underlying,
                              symbol=st.option_symbol, features={"count": st.reprice_count})
            elif st.reprice_count >= int(get(self.cfg, "opt_max_reprices", 6)):
                st.pending_side = None    # give up; retry next cycle (order left resting)

    def resolve_pending(self, st: HedgeState, fill_oid: str) -> bool:
        """Returns True when fill_oid belongs to this sleeve's pending order."""
        matched = st.pending_oid == str(fill_oid)
        if matched:
            st.pending_oid = None
            st.pending_side = None
            st.reprice_count = 0
        return matched

    # ------------------------------------------------------------- orders
    async def _sell(
        self, st: HedgeState, kind: str, contracts: int,
        chain: OptionChain, now: datetime,
    ) -> None:
        from core.models import OrderIntent, OrderType, Side, TimeInForce

        cand = await chain.pick(st.underlying, kind, now)
        if not cand:
            self.on_event("hedge_no_candidate", underlying=st.underlying,
                          features={"kind": kind, "need": contracts})
            return
        bid, ask = cand.bid or Decimal("0"), cand.ask or Decimal("0")
        limit = (bid + ask) / 2 if (bid and ask) else bid
        pact = OrderIntent(
            algo=self.algo, product_symbol=cand.symbol, product_id=cand.product_id,
            side=Side.SELL, size=int(contracts), order_type=OrderType.LIMIT,
            limit_price=limit, time_in_force=TimeInForce.GTC,
            purpose="hedge_open",
            reason=f"sell {kind} covering {st.underlying} lots",
            meta={"underlying": st.underlying, "dte": round(cand.dte, 3),
                  "delta": str(cand.delta)},
        ).with_client_id(self.algo, cand.symbol, "hedge_open")
        res = await self.exec_port.place(pact)
        st.kind = kind
        st.option_symbol = cand.symbol
        st.expiry = parse_expiry(cand.expiry_date)
        st.pending_oid = res.client_order_id
        st.pending_side = "sell"
        st.last_reprice = now

    async def _buy_back(
        self, st: HedgeState, contracts: int, now: datetime, reason: str
    ) -> None:
        from core.models import OrderIntent, OrderType, Side, TimeInForce

        if not st.option_symbol or contracts <= 0:
            return
        t = await self.exec_port.ticker(st.option_symbol)
        limit = (t.best_ask if t and t.best_ask else None)
        pact = OrderIntent(
            algo=self.algo, product_symbol=st.option_symbol,
            side=Side.BUY, size=int(contracts), order_type=OrderType.LIMIT,
            limit_price=limit, time_in_force=TimeInForce.GTC,
            purpose="hedge_close", reason=f"buy back option ({reason})",
            meta={"underlying": st.underlying},
        ).with_client_id(self.algo, st.option_symbol, "hedge_close")
        res = await self.exec_port.place(pact)
        st.pending_oid = res.client_order_id
        st.pending_side = "buy"
        st.last_reprice = now


def _premium(t) -> Optional[float]:
    if t.best_bid is not None and t.best_ask is not None and t.best_bid > 0:
        return float((t.best_bid + t.best_ask) / 2)
    return float(t.mark_price) if t.mark_price is not None else None
