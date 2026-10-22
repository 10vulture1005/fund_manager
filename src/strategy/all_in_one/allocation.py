"""Dynamic capital allocation (spec 2.10).

Daily target weight = base weight x regime_fit x inverse-vol factor,
clamped to [w_min, w_max], renormalised to 1, then smoothed:
    w_new = 0.7 * w_old + 0.3 * w_target

Reallocation only changes the budget for NEW lots; existing lots are never
force-closed by reallocation (de-risk rules do that).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from strategy.all_in_one.defaults import get

SLEEVES = ("eth_cc", "btc_cp", "gold", "stocks")


@dataclass
class Allocation:
    weights: dict[str, float]          # smoothed weights (sum ~ 1.0)
    targets: dict[str, float]          # last raw targets
    budgets: dict[str, float]          # USD deployable per sleeve (NEW lots)
    capital: float = 0.0
    deployable: float = 0.0


def clamp_renormalise(
    weights: dict[str, float], w_min: float, w_max: float
) -> dict[str, float]:
    """Clamp each weight into [w_min, w_max] and renormalise so sum == 1.

    Fixed-point iteration: clamp into the band, renormalise the sum to 1,
    re-clamp, ... until stable (handles over-max and under-min cases).
    """
    names = list(weights)
    w = {n: min(max(float(weights[n] or 0.0), w_min), w_max) for n in names}
    for _ in range(200):
        s = sum(w.values())
        if s <= 0:
            return {n: 1.0 / len(names) for n in names}
        if abs(s - 1.0) < 1e-12:
            break
        w = {n: v / s for n, v in w.items()}     # renormalise
        changed = False
        for n in names:                          # re-clamp into the band
            nv = min(max(w[n], w_min), w_max)
            if abs(nv - w[n]) > 1e-15:
                changed = True
            w[n] = nv
        if not changed:
            break
    # push any remaining float residual into one interior member
    s = sum(w.values())
    if abs(s - 1.0) > 1e-12:
        adj = 1.0 - s
        for n in names:
            if w_min + 1e-12 < w[n] < w_max - 1e-12:
                w[n] += adj
                break
    return {n: min(max(w[n], w_min), w_max) for n in names}


class Allocator:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.w_min = float(get(cfg, "allocation.w_min", 0.05))
        self.w_max = float(get(cfg, "allocation.w_max", 0.40))
        self.cash_reserve_pct = float(get(cfg, "allocation.cash_reserve_pct", 20))
        base_raw = get(cfg, "allocation.base_weights", {}) or {}
        self.base_weights: dict[str, float] = {
            s: float(base_raw.get(s) or 0.0) for s in SLEEVES
        }
        # smoothed weights start at the base (clamped); 0 until first rebalance
        s = sum(self.base_weights.values())
        self.weights: dict[str, float] = {
            s2: (self.base_weights[s2] / s if s > 0 else 1.0 / len(SLEEVES))
            for s2 in SLEEVES
        }
        self.targets: dict[str, float] = dict(self.weights)
        self.capital = float(get(cfg, "allocation.strategy_capital_usd", 0) or 0.0)
        self.regime_fit_table: dict[str, dict[str, float]] = dict(
            get(cfg, "allocation.regime_fit", {}) or {}
        )

    # ------------------------------------------------------------------ parts
    def regime_fit(self, sleeve: str, trend: str) -> float:
        return float(self.regime_fit_table.get(sleeve, {}).get(trend, 1.0))

    def inverse_vol_factor(self, sleeve_vols: dict[str, float]) -> dict[str, float]:
        inv = {}
        for s in SLEEVES:
            v = float(sleeve_vols.get(s, 0.0))
            inv[s] = 1.0 / v if v > 1e-12 else 1.0
        mean_inv = sum(inv.values()) / len(inv)
        if mean_inv <= 0:
            return {s: 1.0 for s in SLEEVES}
        return {s: inv[s] / mean_inv for s in SLEEVES}

    def target_weights(
        self,
        sleeve_trends: dict[str, str],
        sleeve_vols: dict[str, float] | None = None,
    ) -> dict[str, float]:
        raw: dict[str, float] = {}
        ivf = self.inverse_vol_factor(sleeve_vols or {})
        for s in SLEEVES:
            base = self.base_weights[s]
            if base <= 0:
                raw[s] = 0.0
                continue
            fit = self.regime_fit(s, sleeve_trends.get(s, "neutral"))
            raw[s] = base * fit * ivf[s]
        w = {s: min(max(raw[s], self.w_min), self.w_max) for s in SLEEVES}
        return clamp_renormalise(w, self.w_min, self.w_max)

    # ------------------------------------------------------------------ daily
    def rebalance(
        self,
        sleeve_trends: dict[str, str],
        sleeve_vols: dict[str, float] | None = None,
        capital: float | None = None,
    ) -> Allocation:
        self.targets = self.target_weights(sleeve_trends, sleeve_vols)
        self.weights = {
            s: 0.7 * self.weights[s] + 0.3 * self.targets[s] for s in SLEEVES
        }
        if capital is not None:
            self.capital = float(capital)
        return self.allocation()

    def allocation(self) -> Allocation:
        deployable = self.capital * (1.0 - self.cash_reserve_pct / 100.0)
        budgets = {s: deployable * self.weights[s] for s in SLEEVES}
        return Allocation(
            weights=dict(self.weights),
            targets=dict(self.targets),
            budgets=budgets,
            capital=self.capital,
            deployable=deployable,
        )

    # ----------------------------------------------------------------- lots
    def budget_for_new_lot(
        self,
        sleeve: str,
        max_lots: int,
        high_vol: bool = False,
        n_symbols: int = 1,
    ) -> float:
        """Budget for ONE NEW lot (spec 2.6/2.10). Existing lots unaffected."""
        alloc = self.allocation()
        if max_lots <= 0 or n_symbols <= 0:
            return 0.0
        budget = alloc.budgets.get(sleeve, 0.0)
        # stocks (spec 2.8): lot size = stock_sleeve_budget / (max_lots * n_symbols)
        per_lot = budget / max_lots if n_symbols == 1 else budget / (max_lots * n_symbols)
        if high_vol:
            per_lot *= 0.5
        return per_lot
