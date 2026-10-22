"""Profit booking (spec 2.9): the "≈20% of used margin" rule.

- Target: close the campaign when unrealised PnL >= profit_target_pct_of_margin
  % of USED margin. Equivalent price move from avg entry = pct/100 / leverage.
- Express as bracket `take_profit` and re-centre after every rung fill; ALSO
  check in software on every mark-price tick (bracket may be missed).
- tp_close_fraction default 1.0 (close all); if < 1.0, close that fraction and
  trail the remainder with trail_atr_mult x ATR(1H).
- Time stop: campaign older than max_campaign_days.

Pure functions, unit-testable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def effective_move_pct(target_pct_of_margin: float, leverage: float) -> float:
    """Price move (fraction, e.g. 0.04 for 4%) that returns
    target_pct_of_margin% on margin at `leverage`."""
    if leverage <= 0:
        raise ValueError("leverage must be > 0")
    return target_pct_of_margin / 100.0 / leverage


def effective_leverage_map(cfg: dict[str, Any]) -> dict[str, float]:
    """Symbol -> leverage preference, honouring the hard cap `max_leverage`."""
    cap = float(cfg.get("max_leverage", 10))
    lev = cfg.get("leverage")
    out = {}
    for s in cfg.get("symbols", []):
        v = lev
        if isinstance(lev, dict):
            v = lev.get(s, lev.get("default"))
        if v is None:
            continue
        out[s] = min(float(v), cap)
    return out


def used_margin(notional: Any, leverage: float) -> float:
    """Used margin = notional (USD) / effective leverage.

    To compute from contracts instead, use `used_margin_contracts`.
    """
    return float(notional) / leverage if leverage > 0 else 0.0


def used_margin_contracts(contracts: int, price: float, leverage: float,
                          contract_value: float = 1.0) -> float:
    if leverage <= 0:
        return 0.0
    notional = abs(contracts) * float(price) * contract_value
    return notional / leverage


def unrealised_pnl(direction: str, avg_entry: float, mark: float,
                   contracts: int, contract_value: float = 1.0) -> float:
    sgn = 1.0 if direction == "long" else -1.0
    return (mark - avg_entry) * contracts * contract_value * sgn


def target_hit(unrealised_pnl_value: float, used_margin_value: float,
               target_pct_of_margin: float) -> bool:
    """Software backup TP check: pnl >= pct% of used margin (margin > 0)."""
    if used_margin_value <= 0:
        return False
    return unrealised_pnl_value >= used_margin_value * target_pct_of_margin / 100.0


def bracket_tp_price(avg_entry: float, direction: str, leverage: float,
                     target_pct_of_margin: float) -> float:
    mv = effective_move_pct(target_pct_of_margin, leverage)
    return avg_entry * (1.0 + mv) if direction == "long" else avg_entry * (1.0 - mv)


def trail_stop_price(price: float, atr: float, trail_atr_mult: float,
                     direction: str, current_stop: float | None = None) -> float:
    """New trailing stop for the open remainder (`tp_close_fraction < 1`)."""
    if direction == "long":
        candidate = price - trail_atr_mult * atr
        return candidate if current_stop is None else max(candidate, current_stop)
    candidate = price + trail_atr_mult * atr
    return candidate if current_stop is None else min(candidate, current_stop)


def time_stop_hit(created_ts: Any, now_ts: Any, max_campaign_days: float) -> bool:
    """Time stop (2.9): close if the campaign is older than max_campaign_days."""
    if created_ts is None:
        return False
    c = created_ts if isinstance(created_ts, datetime) else datetime.fromisoformat(str(created_ts))
    n = now_ts if isinstance(now_ts, datetime) else datetime.fromisoformat(str(now_ts))
    if c.tzinfo is None:
        c = c.replace(tzinfo=timezone.utc)
    if n.tzinfo is None:
        n = n.replace(tzinfo=timezone.utc)
    age_days = (n - c).total_seconds() / 86400.0
    return age_days >= max_campaign_days
