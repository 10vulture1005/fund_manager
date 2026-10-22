"""test_aio_allocation — weights clamp/renormalise/smooth math exactness
(spec 2.10) + budgets per NEW lot."""
from __future__ import annotations

import pytest

from strategy.all_in_one import DEFAULTS
from strategy.all_in_one.allocation import (
    Allocator,
    SLEEVES,
    clamp_renormalise,
)


def cfg_with(capital: float = 100_000.0, **overrides) -> dict:
    import copy

    cfg = copy.deepcopy(DEFAULTS)
    cfg["allocation"] = {**copy.deepcopy(DEFAULTS["allocation"]), **overrides}
    if capital is not None:
        cfg["allocation"]["strategy_capital_usd"] = capital
    return cfg


def test_clamp_renormalise_simple():
    w = {"a": 0.5, "b": 0.3, "c": 0.1, "d": 0.1}
    out = clamp_renormalise(w, 0.05, 0.40)
    # a is clamped to 0.40; b/c/d scaled so the total is exactly 1
    assert out["a"] == pytest.approx(0.40)
    assert out["b"] == pytest.approx(0.3 * 0.6 / 0.5, rel=1e-9)
    assert out["c"] == pytest.approx(0.1 * 0.6 / 0.5, rel=1e-9)
    assert out["d"] == pytest.approx(0.1 * 0.6 / 0.5, rel=1e-9)
    assert sum(out.values()) == pytest.approx(1.0, abs=1e-12)


def test_clamp_renormalise_everything_out_of_range():
    w = {"a": 2.0, "b": 2.0, "c": 2.0, "d": 2.0}
    out = clamp_renormalise(w, 0.05, 0.40)
    for v in out.values():
        assert v == pytest.approx(0.25, rel=1e-9)   # all equal inside the band
    assert sum(out.values()) == pytest.approx(1.0, abs=1e-12)


def test_clamp_renormalise_respects_floor():
    w = {"a": 0.97, "b": 0.01, "c": 0.01, "d": 0.01}
    out = clamp_renormalise(w, 0.05, 0.40)
    for v in out.values():
        assert 0.05 - 1e-12 <= v <= 0.40 + 1e-12


def test_target_weights_equal_vols_no_fit_change():
    cfg = cfg_with(base_weights={
        "eth_cc": 0.30, "btc_cp": 0.20, "gold": 0.25, "stocks": 0.25,
    })
    al = Allocator(cfg)
    trends = {s: "neutral" for s in SLEEVES}
    vols = {s: 1.0 for s in SLEEVES}       # equal vols -> ivf all 1.0
    targets = al.target_weights(trends, vols)
    # neutral fit per the default table: eth 0.8, btc 0.8, gold 1.0, stocks 0.6
    exp = {"eth_cc": 0.30 * 0.8, "btc_cp": 0.20 * 0.8,
           "gold": 0.25 * 1.0, "stocks": 0.25 * 0.6}
    # no value exceeds the band, so renormalisation only rescales:
    total = sum(exp.values())
    for s in SLEEVES:
        assert targets[s] == pytest.approx(exp[s] / total, rel=1e-9)
    assert sum(targets.values()) == pytest.approx(1.0, abs=1e-12)


def test_regime_fit_zero_still_gets_floor():
    cfg = cfg_with(base_weights={s: 0.25 for s in SLEEVES})
    al = Allocator(cfg)
    # eth_cc fit is 0 in strong_down
    fits = al.regime_fit("eth_cc", "strong_down")
    assert fits == 0.0
    raw = {s: (0.0 if s == "eth_cc" else al.base_weights[s]) for s in SLEEVES}
    w = clamp_renormalise({s: min(max(raw[s], 0.05), 0.40) for s in SLEEVES}, 0.05, 0.40)
    # eth_cc keeps only the renormalised floor share of the book
    assert w["eth_cc"] == pytest.approx(1.0 / 16.0)   # 0.05 clamped then renormalised
    assert w["eth_cc"] < 0.10 < w["btc_cp"]
    assert sum(w.values()) == pytest.approx(1.0, abs=1e-12)


def test_inverse_vol_factor_math():
    al = Allocator(cfg_with())
    ivf = al.inverse_vol_factor({"eth_cc": 0.02, "btc_cp": 0.04,
                                 "gold": 0.02, "stocks": 0.02})
    invs = {s: 1.0 / v for s, v in
            (("eth_cc", 0.02), ("btc_cp", 0.04), ("gold", 0.02), ("stocks", 0.02))}
    mean = sum(invs.values()) / 4
    for s in SLEEVES:
        assert ivf[s] == pytest.approx(invs[s] / mean, rel=1e-9)
    # a more volatile sleeve gets LESS weight factor
    assert ivf["btc_cp"] < ivf["eth_cc"]


def test_smoothing_is_07_old_03_target():
    cfg = cfg_with(base_weights={s: 0.25 for s in SLEEVES})
    al = Allocator(cfg)
    old = dict(al.weights)
    new_targets = al.rebalance(
        sleeve_trends={s: "up" for s in SLEEVES},
        sleeve_vols={s: 1.0 for s in SLEEVES},
    )
    _ = new_targets
    for s in SLEEVES:
        assert al.weights[s] == pytest.approx(
            0.7 * old[s] + 0.3 * al.targets[s], rel=1e-12
        )
    assert sum(al.weights.values()) == pytest.approx(1.0, abs=1e-12)


def test_budgets_cash_reserve_and_new_lot():
    cfg = cfg_with(capital=100_000.0, base_weights={s: 0.25 for s in SLEEVES})
    al = Allocator(cfg)
    al.rebalance(sleeve_trends={s: "neutral" for s in SLEEVES},
                 sleeve_vols={s: 1.0 for s in SLEEVES})
    alloc = al.allocation()
    assert alloc.deployable == pytest.approx(80_000.0)   # 20% cash reserve kept
    # budgets equal smoothed weight x deployable
    for s in SLEEVES:
        assert alloc.budgets[s] == pytest.approx(80_000.0 * al.weights[s], abs=1e-6)
    den = sum(alloc.budgets.values())
    assert den == pytest.approx(80_000.0, rel=1e-9)

    # a NEW lot budget: sleeve budget / max_lots
    per_lot = al.budget_for_new_lot("eth_cc", max_lots=5)
    assert per_lot == pytest.approx(alloc.budgets["eth_cc"] / 5, rel=1e-9)
    # high-vol regime halves new-lot sizes (spec 2.9)
    per_lot_hv = al.budget_for_new_lot("eth_cc", max_lots=5, high_vol=True)
    assert per_lot_hv == pytest.approx(per_lot / 2.0, rel=1e-9)
    # stocks: budget / (max_lots * n_symbols)
    st = al.budget_for_new_lot("stocks", max_lots=5, n_symbols=3)
    assert st == pytest.approx(alloc.budgets["stocks"] / (5 * 3), rel=1e-9)
    # existing lots are never touched: budgets only feed NEW lots
    assert al.allocation().budgets == alloc.budgets
