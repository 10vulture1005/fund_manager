"""Ladder math: budget -> rung sizes, invalidation, risk cap (prompt A 2.7-2.8)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strategy.rwa_positional.campaign import (
    blended_entry,
    campaign_budget,
    contracts_for,
    plan_ladder,
    plan_rung_prices,
)


def _plan(**kw):
    base = dict(
        direction="long", e1_price=100.0, atr_1h_value=1.0,
        invalidation_price=94.0, equity=100_000.0, leverage=5.0,
        weights=[0.25, 0.25, 0.25, 0.25], rungs=4,
        ladder_atr_mult=[0.6, 1.2, 1.8], ladder_mode="against",
        campaign_margin_pct=15.0, campaign_risk_pct=1.0,
        contract_value=1.0, target_pct_of_margin=20.0,
    )
    base.update(kw)
    return plan_ladder(**base)


def test_rung_prices_drop_beyond_invalidation():
    prices = plan_rung_prices(
        e1_price=100.0, atr_1h_value=1.0, direction="long",
        ladder_mode="against", ladder_atr_mult=[0.6, 1.2, 1.8],
        invalidation_price=99.0,
    )
    # 100 - 0.6 = 99.4 > 99.0 kept; 100 - 1.2 = 98.8 <= 99.0 MUST be dropped
    assert prices == [99.4]
    prices_all = plan_rung_prices(
        e1_price=100.0, atr_1h_value=1.0, direction="long",
        ladder_mode="against", ladder_atr_mult=[0.6, 1.2, 1.8],
        invalidation_price=94.0,
    )
    assert prices_all == [99.4, 98.8, 98.2]


def test_rung_prices_with_mode():
    up = plan_rung_prices(e1_price=100.0, atr_1h_value=1.0, direction="long",
                          ladder_mode="with",
                          ladder_atr_mult=[0.6, 1.2],
                          invalidation_price=94.0)
    assert up == [100.6, 101.2]


def test_contracts_sizing_rule():
    assert contracts_for(500.0, 5.0, 100.0, 1.0) == 25
    assert contracts_for(499.999, 5.0, 100.0, 1.0) == 24
    assert contracts_for(0.0, 5.0, 100.0, 1.0) == 0


def test_campaign_budget_min_of_pct_and_risk_implied():
    # rel risk 6% at 5x: risk-implied budget = 1000 / (5*0.06) = 3333 USD
    budget, cap = campaign_budget(100_000.0, 15.0, 1.0, 5.0, 0.06)
    assert cap == 1000.0
    assert budget == pytest.approx(3333.333, rel=1e-4)


def test_ladder_risk_cap_respected():
    plan = _plan()
    assert plan.skipped_reason is None
    assert len(plan.rungs) == 4
    assert plan.rungs[0].price == 100.0
    assert [r.price for r in plan.rungs[1:]] == [99.4, 98.8, 98.2]
    assert all(r.price > 94.0 for r in plan.rungs)      # never beyond invalidation
    assert all(r.contracts > 0 for r in plan.rungs)
    # worst case: ALL rungs filled + SL -> loss <= campaign_risk_pct of equity
    assert plan.weighted_risk <= 100_000.0 * 0.01 + 1e-6


def test_ladder_margin_pct_budget_when_risk_cap_allows_it():
    # with a loose risk cap the pct-of-equity budget wins
    plan = _plan(campaign_risk_pct=10.0, weights=[0.25, 0.25, 0.25, 0.25])
    assert plan.skipped_reason is None
    assert plan.budget_margin == pytest.approx(15_000.0)
    # still no rung beyond invalidation
    assert all(r.price > 94.0 for r in plan.rungs)
    assert all(r.contracts > 0 for r in plan.rungs)


def test_ladder_respects_cap_with_nonuniform_weights():
    plan = _plan(weights=[0.1, 0.3, 0.3, 0.3])
    assert plan.weighted_risk <= 100_000.0 * 0.01 + 1e-6


def test_ladder_skips_when_rung1_cannot_satisfy_cap():
    # tiny equity: rung-1's floor contracts == 0 -> skip the signal
    plan = _plan(equity=10_000.0, campaign_margin_pct=15.0, campaign_risk_pct=0.02)
    assert plan.skipped_reason == "rung1_below_min_size_risk_cap"
    assert plan.rungs == []


def test_tp_price_and_blended_entry():
    plan = _plan()
    # TP: target 20% of margin at 5x -> 4% price move from e1
    assert abs(plan.tp_price - 104.0) < 1e-9
    fills = [(75, 100.0), (76, 99.0)]
    assert abs(blended_entry(fills) - (75 * 100 + 76 * 99) / 151) < 1e-9
