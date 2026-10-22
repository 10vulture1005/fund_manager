"""Profit booking math (prompt A 2.9): the "≈20% of used margin" rule."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from strategy.rwa_positional.profit_booking import (
    bracket_tp_price,
    effective_leverage_map,
    effective_move_pct,
    target_hit,
    time_stop_hit,
    trail_stop_price,
    unrealised_pnl,
    used_margin,
    used_margin_contracts,
)


def test_price_move_to_target_is_pct_of_margin_over_leverage():
    # 20% of margin target at 5x == 4% price move; at 10x == 2%
    assert effective_move_pct(20.0, 5.0) == 0.04
    assert effective_move_pct(20.0, 10.0) == 0.02
    assert effective_move_pct(10.0, 5.0) == 0.02


def test_effective_leverage_map_and_cap():
    cfg = {"symbols": ["XAUTUSD", "SLVONUSD"], "leverage": 20.0,
           "max_leverage": 10, "leverage": 20.0}
    cfg2 = {"symbols": ["XAUTUSD", "PAXGUSD"],
            "leverage": {"XAUTUSD": 5, "PAXGUSD": 20}}
    m = effective_leverage_map(cfg)
    assert m["XAUTUSD"] == 10.0  # hard cap
    m2 = effective_leverage_map(cfg2)
    assert m2 == {"XAUTUSD": 5.0, "PAXGUSD": 10.0}


def test_bracket_tp_price_directional():
    assert abs(bracket_tp_price(100.0, "long", 5.0, 20.0) - 104.0) < 1e-9
    assert abs(bracket_tp_price(100.0, "short", 5.0, 20.0) - 96.0) < 1e-9


def test_target_hit_software_backup():
    # 100 contracts @100, lev 10 => notional 10k, margin 1k; 20% target = 200
    margin = used_margin_contracts(100, 100.0, 10.0)
    assert abs(margin - 1000.0) < 1e-9
    pnl = unrealised_pnl("long", 100.0, 102.0, 100)          # +2*100 = 200
    assert target_hit(pnl, margin, 20.0) is True
    pnl2 = unrealised_pnl("long", 100.0, 101.9, 100)          # 190 < 200
    assert target_hit(pnl2, margin, 20.0) is False
    # short mirror
    pnl3 = unrealised_pnl("short", 100.0, 98.0, 100)
    assert target_hit(pnl3, margin, 20.0) is True


def test_used_margin_from_notional():
    assert abs(used_margin(10_000.0, 10.0) - 1000.0) < 1e-9
    assert used_margin(10_000.0, 0.0) == 0.0


def test_trailing_stop_monotone():
    assert abs(trail_stop_price(110.0, 2.0, 1.0, "long") - 108.0) < 1e-9
    # never lowers the stop
    assert abs(trail_stop_price(110.0, 2.0, 1.0, "long", 109.0) - 109.0) < 1e-9
    # short mirrors below price
    assert abs(trail_stop_price(110.0, 2.0, 1.0, "short") - 112.0) < 1e-9


def test_time_stop_hit():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert time_stop_hit(start, start + timedelta(days=13, hours=23), 14) is False
    assert time_stop_hit(start, start + timedelta(days=14), 14) is True
    assert time_stop_hit(None, start, 14) is False
