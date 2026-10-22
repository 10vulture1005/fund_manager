"""Campaign = scale-in ladder (spec 2.7) + risk-capped sizing (spec 2.8).

Pure, unit-testable math plus a Campaign container the strategy mutates.

- Rung 1: entry on signal (post-only limit at best bid/ask; IOC cross fallback
  after `entry_timeout_s`; never a pure market order unless `allow_market`).
- Rungs 2..N: resting post-only limits at E1 ∓ k x ATR(1H) for k in
  ladder_atr_mult (`against` mode: adverse direction, averaging into the
  zone; `with`: pyramid in trade direction). HARD RULE: no rung may sit
  beyond the invalidation price.
- Rung weights of the campaign margin budget (equal by default;
  martingale_multiplier key exists, defaults to 1.0, warn when > 1.0).
- Risk cap: if ALL rungs fill and the SL (invalidation) hits, realised loss
  <= campaign_risk_pct % of account equity => scale rung margins down;
  if rung 1 alone cannot satisfy the cap, skip the signal.
"""
from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Direction = Literal["long", "short"]


def warn_martingale(cfg: dict[str, Any]) -> float:
    mm = float(cfg.get("martingale_multiplier", 1.0) or 1.0)
    if mm > 1.0:
        warnings.warn(
            "rwa_positional: martingale_multiplier %.2f > 1.0 — rung weights become "
            "progressive (a mild martingale). The default is 1.0 (equal weights)."
            % mm,
            stacklevel=2,
        )
    return mm


@dataclass
class Rung:
    index: int                 # 1-based
    price: float
    contracts: int
    weight: float              # share of the campaign margin budget
    margin: float              # margin allocated to this rung (post scaling)
    post_only: bool = True
    status: str = "planned"    # planned | placed | filled | cancelled | crossed
    client_order_id: str = ""

    @property
    def notional(self) -> float:
        return self.contracts * self.price


@dataclass
class LadderPlan:
    direction: Direction
    budget_margin: float
    rungs: list[Rung]
    e1_price: float
    sl_price: float            # = invalidation price = exchange bracket SL
    tp_price: float
    invalidation_price: float
    leverage: float
    scaled: bool = False
    skipped_reason: Optional[str] = None
    full_risk: float = 0.0     # risk at unscaled margins (USD)
    target_pct_of_margin: float = 20.0

    @property
    def contracts(self) -> int:
        return sum(r.contracts for r in self.rungs)

    @property
    def weighted_risk(self) -> float:
        """USD loss if ALL rungs fill and the SL hits, at planned sizes."""
        lev = self.leverage
        return sum(
            r.margin * lev * abs(r.price - self.sl_price) / r.price
            for r in self.rungs
            if r.contracts > 0
        )


def campaign_budget(
    equity: float,
    campaign_margin_pct: float,
    campaign_risk_pct: float,
    leverage: float,
    rel_risk: float,
) -> tuple[float, float]:
    """Campaign margin budget (2.10): min(campaign_margin_pct% of equity, the
    amount implied by the risk cap). Returns (budget, risk_cap_usd).

    rel_risk = the RELATIVE price distance from the first rung to the SL
    (worst starting worst case); the budget implied by the risk cap is
    (risk_pct% equity) / (leverage * rel_risk).
    """
    risk_cap = equity * campaign_risk_pct / 100.0
    implied = risk_cap / (leverage * rel_risk) if (leverage > 0 and rel_risk > 0) else float("inf")
    pct_budget = equity * campaign_margin_pct / 100.0
    budget = min(pct_budget, implied)
    return budget, risk_cap


def plan_rung_prices(
    *,
    e1_price: float,
    atr_1h_value: float,
    direction: Direction,
    ladder_atr_mult: list[float],
    ladder_mode: str = "against",
    invalidation_price: float,
    tick: float = 1e-9,
) -> list[float]:
    """Prices for rungs 2..N. `against`: adverse (average into the zone);
    `with`: pyramid in trade direction. Any price at/beyond the invalidation
    price is DROPPED (hard rule: never beyond)."""
    atrv = max(atr_1h_value, tick)
    sign = 1.0 if direction == "long" else -1.0
    if ladder_mode == "with":
        sign *= -1.0  # `with` places rungs in the trade direction
    prices: list[float] = []
    for k in ladder_atr_mult:
        p = e1_price - sign * k * atrv  # `- sign` because adverse = against entry
        if ladder_mode == "with":
            p = e1_price + (k * atrv if direction == "long" else -k * atrv)
        if direction == "long":
            if p <= invalidation_price:
                continue
        else:
            if p >= invalidation_price:
                continue
        prices.append(round(p, 10))
    return prices


def contracts_for(
    margin: float, leverage: float, price: float, contract_value: float
) -> int:
    """Spec 1.6 sizing: floor(target_notional / (price x contract_value))."""
    if price <= 0 or contract_value <= 0 or margin <= 0 or leverage <= 0:
        return 0
    notional = margin * leverage
    return int(math.floor(notional / (price * contract_value)))


def plan_ladder(
    *,
    direction: Direction,
    e1_price: float,
    atr_1h_value: float,
    invalidation_price: float,
    equity: float,
    leverage: float,
    weights: list[float] | None = None,
    rungs: int = 4,
    ladder_atr_mult: list[float] | None = None,
    ladder_mode: str = "against",
    campaign_margin_pct: float = 15.0,
    campaign_risk_pct: float = 1.0,
    contract_value: float = 1.0,
    tick: float = 1e-9,
    target_pct_of_margin: float = 20.0,
) -> LadderPlan:
    """Build the full ladder plan with risk-capped sizing (spec 2.7-2.9)."""
    ladder_atr_mult = ladder_atr_mult or [0.6, 1.2, 1.8]
    weights = list(weights) if weights else None
    if not weights or len(weights) != rungs:
        weights = [1.0 / rungs] * rungs
    tw = sum(weights)
    weights = [w / tw for w in weights]

    sl_price = invalidation_price
    rel_risk = abs(e1_price - sl_price) / e1_price
    budget, risk_cap = campaign_budget(
        equity, campaign_margin_pct, campaign_risk_pct, leverage, rel_risk
    )
    if budget <= 0 or rel_risk <= 0:
        return LadderPlan(
            direction=direction, budget_margin=0.0, rungs=[], e1_price=e1_price,
            sl_price=sl_price, tp_price=e1_price, invalidation_price=invalidation_price,
            leverage=leverage, skipped_reason="zero_budget_or_relrisk",
        )

    # unscaled margins and risk
    margins = [budget * w for w in weights]
    rung_prices = [e1_price] + plan_rung_prices(
        e1_price=e1_price,
        atr_1h_value=atr_1h_value,
        direction=direction,
        ladder_atr_mult=ladder_atr_mult,
        ladder_mode=ladder_mode,
        invalidation_price=invalidation_price,
        tick=tick,
    )
    rung_prices = rung_prices[:rungs]
    # pad with no extra rungs if ladder prices fewer than needed
    full_risk = sum(
        m * leverage * abs(p - sl_price) / p
        for m, p in zip(margins[: len(rung_prices)], rung_prices)
    )

    scaled = full_risk > risk_cap
    scale = min(1.0, risk_cap / full_risk) if full_risk > 0 else 0.0
    margins_s = [m * scale for m in margins[: len(rung_prices)]]

    out_rungs: list[Rung] = []
    for i, (p, w, m) in enumerate(zip(rung_prices, weights, margins_s), start=1):
        contracts = contracts_for(m, leverage, p, contract_value)
        if contracts <= 0:
            if i == 1:
                # rung 1 alone cannot satisfy the risk cap -> skip the signal
                return LadderPlan(
                    direction=direction, budget_margin=budget, rungs=[],
                    e1_price=e1_price, sl_price=sl_price, tp_price=e1_price,
                    invalidation_price=invalidation_price, leverage=leverage,
                    scaled=scaled, full_risk=full_risk,
                    skipped_reason="rung1_below_min_size_risk_cap",
                )
            continue
        out_rungs.append(
            Rung(index=i, price=p, contracts=contracts, weight=w, margin=m,
                 post_only=True)
        )
    plan = LadderPlan(
        direction=direction,
        budget_margin=budget,
        rungs=out_rungs,
        e1_price=e1_price,
        sl_price=sl_price,
        tp_price=e2tp(e1_price, direction, leverage, target_pct_of_margin),
        invalidation_price=invalidation_price,
        leverage=leverage,
        scaled=scaled,
        full_risk=full_risk,
        target_pct_of_margin=target_pct_of_margin,
    )
    return plan


def e2tp(entry: float, direction: Direction, leverage: float,
         target_pct_of_margin: float) -> float:
    """TP price: target_pct_of_margin% of margin at `leverage` =>
    price move = pct/100 / leverage."""
    mv = target_pct_of_margin / 100.0 / leverage
    return entry * (1 + mv) if direction == "long" else entry * (1 - mv)


def tp_price_for(entry: float, direction: Direction, leverage: float,
                 target_pct_of_margin: float) -> float:
    return e2tp(entry, direction, leverage, target_pct_of_margin)


def blended_entry(fills: list[tuple[int, float]]) -> float:
    """Avg entry from (size, price) fills of the SAME side."""
    tot = sum(s for s, _ in fills)
    if tot <= 0:
        return 0.0
    return sum(s * p for s, p in fills) / tot


@dataclass
class Campaign:
    """One directional campaign (bookkeeping lives HERE + in the SQLite ledger,
    never as a second exchange position — Delta nets one product to one net
    position)."""

    id: int | None
    symbol: str
    direction: Direction
    zone_price: float
    zone_score: int
    zone_upper: float
    zone_lower: float
    invalidation_price: float
    sl_price: float
    leverage: float
    budget_margin: float
    weights: list[float]
    target_pct_of_margin: float
    contract_value: float = 1.0
    tick: float = 0.01
    created_ts: Any = None
    rungs: list[Rung] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)  # {rung_index,size,price,ts,lot_id}
    tp_price: float = 0.0
    exit_reason: str | None = None
    exit_ts: Any = None
    trailing: bool = False
    features: dict[str, Any] = field(default_factory=dict)

    # ---- stats -----------------------------------------------------------
    @property
    def filled_fills(self) -> list[dict[str, Any]]:
        return self.fills

    @property
    def contracts(self) -> int:
        return sum(f["size"] for f in self.fills)

    @property
    def avg_entry(self) -> float:
        f = self.fills
        if not f:
            return 0.0
        tot = sum(x["size"] for x in f)
        return sum(x["size"] * x["price"] for x in f) / tot

    @property
    def notional(self) -> float:
        return sum(x["size"] * x["price"] for x in self.fills)

    @property
    def margin_used(self) -> float:
        return self.notional / self.leverage if self.leverage > 0 else 0.0

    def rung_by_index(self, index: int) -> Rung | None:
        for r in self.rungs:
            if r.index == index:
                return r
        return None

    @property
    def unfilled_rungs(self) -> list[Rung]:
        return [r for r in self.rungs if r.status in ("planned", "placed")]

    @property
    def all_rungs_filled(self) -> bool:
        return all(
            r.status == "filled" for r in self.rungs
        ) and bool(self.rungs)

    def unrealised_pnl(self, mark: float) -> float:
        if not self.fills:
            return 0.0
        sgn = 1.0 if self.direction == "long" else -1.0
        return (mark - self.avg_entry) * self.contracts * self.contract_value * sgn

    def recentre_tp_sl(self, target_pct_of_margin: float | None = None) -> tuple[float, float]:
        if self.avg_entry <= 0:
            return self.sl_price, self.tp_price
        pct = self.target_pct_of_margin if target_pct_of_margin is None else target_pct_of_margin
        self.tp_price = e2tp(self.avg_entry, self.direction, self.leverage, pct)
        return self.sl_price, self.tp_price


def plan_to_campaign(
    plan: LadderPlan,
    *,
    campaign_id: int | None,
    symbol: str,
    zone_price: float,
    zone_score: int,
    zone_upper: float,
    zone_lower: float,
    contract_value: float,
    tick: float,
    target_pct_of_margin: float,
    created_ts: Any = None,
    rung_ttl_hours: float | None = None,
    entry_timeout_s: float | None = None,
    features: dict | None = None,
) -> Campaign:
    c = Campaign(
        id=campaign_id,
        symbol=symbol,
        direction=plan.direction,
        zone_price=zone_price,
        zone_score=zone_score,
        zone_upper=zone_upper,
        zone_lower=zone_lower,
        invalidation_price=plan.invalidation_price,
        sl_price=plan.sl_price,
        leverage=plan.leverage,
        budget_margin=plan.budget_margin,
        weights=[r.weight for r in plan.rungs],
        target_pct_of_margin=target_pct_of_margin,
        contract_value=contract_value,
        tick=tick,
        created_ts=created_ts,
        rungs=[r for r in plan.rungs],
        tp_price=plan.tp_price,
        features={
            "planned": {
                str(r.index): {"price": r.price, "contracts": r.contracts,
                               "weight": r.weight, "margin": r.margin}
                for r in plan.rungs
            },
            **(features or {}),
        },
    )
    set_target_pct_attr(c, target_pct_of_margin)
    return c


def set_target_pct_attr(campaign: Campaign, pct: float) -> None:
    campaign.target_pct_of_margin = pct


# ------------------------------------------------------- ledger persistence
def campaign_to_meta(c: "Campaign") -> dict[str, Any]:
    return {
        "zone": {"price": c.zone_price, "score": c.zone_score,
                 "upper": c.zone_upper, "lower": c.zone_lower},
        "direction": c.direction,
        "invalidation_price": c.invalidation_price,
        "sl_price": c.sl_price,
        "tp_price": c.tp_price,
        "leverage": c.leverage,
        "budget_margin": c.budget_margin,
        "weights": list(c.weights),
        "target_pct_of_margin": c.target_pct_of_margin,
        "contract_value": c.contract_value,
        "tick": c.tick,
        "created_ts": str(c.created_ts) if c.created_ts else None,
        "rungs": [
            {"index": r.index, "price": r.price, "contracts": r.contracts,
             "weight": r.weight, "margin": r.margin, "post_only": r.post_only,
             "status": r.status, "client_order_id": r.client_order_id}
            for r in c.rungs
        ],
        "fills": [dict(f) for f in c.fills],
        "exit_reason": c.exit_reason,
        "trailing": c.trailing,
        "features": c.features,
    }


def campaign_from_meta(campaign_id: int, symbol: str, meta: dict[str, Any]) -> "Campaign":
    zone = meta.get("zone") or {}
    cam = Campaign(
        id=campaign_id,
        symbol=symbol,
        direction=meta.get("direction", "long"),
        zone_price=float(zone.get("price", 0.0) or 0.0),
        zone_score=int(zone.get("score", 0) or 0),
        zone_upper=float(zone.get("upper", 0.0) or 0.0),
        zone_lower=float(zone.get("lower", 0.0) or 0.0),
        invalidation_price=float(meta.get("invalidation_price") or 0.0),
        sl_price=float(meta.get("sl_price") or 0.0),
        leverage=float(meta.get("leverage") or 1.0),
        budget_margin=float(meta.get("budget_margin") or 0.0),
        weights=list(meta.get("weights") or []),
        target_pct_of_margin=float(meta.get("target_pct_of_margin", 20.0)),
        contract_value=float(meta.get("contract_value", 1.0)),
        tick=float(meta.get("tick", 0.01)),
        created_ts=meta.get("created_ts"),
        rungs=[
            Rung(index=r["index"], price=float(r["price"]),
                 contracts=int(r["contracts"]), weight=float(r.get("weight", 0.0)),
                 margin=float(r.get("margin", 0.0)), post_only=bool(r.get("post_only", True)),
                 status=r.get("status", "planned"),
                 client_order_id=r.get("client_order_id", ""))
            for r in (meta.get("rungs") or [])
        ],
        fills=list(meta.get("fills") or []),
        tp_price=float(meta.get("tp_price") or 0.0),
        exit_reason=meta.get("exit_reason"),
        trailing=bool(meta.get("trailing", False)),
        features=meta.get("features") or {},
    )
    return cam
