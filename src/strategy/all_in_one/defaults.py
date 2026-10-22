"""Config defaults for the `all_in_one` strategy subtree (spec 2.5-2.10).

Every parameter in the spec is a config key. `_ASK` values default to `null`
and are enforced by `core.config.missing_ask_keys` — they block live trading.

Dotted paths consumed by this strategy (see README_note.md for the full list).
"""
from __future__ import annotations

from typing import Any, Optional

ASK_NONE: Optional[float] = None

DEFAULTS: dict[str, Any] = {
    # ---- global behaviour --------------------------------------------------
    "lifo_strict": True,            # spec 2.4: strictly newest-lot-first exits
    "margin_mode_required": "cross",  # spec 2.3 hard requirement (react to infra)
    "flatten_on_kill": True,
    "flat_recheck_minutes": 60,     # hourly cross-margin / reconciliation checks
    "options_cycle_minutes": 15,    # spec 2.11
    "buffer_candles_1d": 400,       # warmup 1D bars requested (EMA200 + 90d vol pct)
    "buffer_candles_4h": 200,
    # ---- global option-leg parameters (spec 2.5) ---------------------------
    "opt_min_dte": 5,
    "opt_max_dte": 10,
    "opt_target_delta": 0.20,
    "opt_delta_band": [0.12, 0.30],
    "opt_max_spread_pct": 0.08,     # 8% as a fraction of the option mid
    "opt_min_oi": 100.0,            # spec says "oi >= opt_min_oi" without a default; ASSUMPTION
    "opt_reprice_s": 10,
    "opt_max_reprices": 6,
    "opt_buyback_capture_pct": 80,  # buy back when 80% of the premium decayed
    "opt_roll_delta": 0.45,
    "iv_premium": 1.1,              # used by the backtest BS approximation (spec 2.12)
    "hedge_style": "standard",      # short options against perp lots (see ASSUMPTIONS note)
    # ---- sleeve: ETH_CC (spec 2.6) ----------------------------------------
    "eth_cc": {
        "enabled": True,
        "symbol": "ETHUSD",
        "lot_size_notional": ASK_NONE,   # [ASK] USD per lot
        "max_lots": 5,
        "sleeve_leverage_cap": 3,
        "lot_tp_pct": 2.0,               # lot TP: entry * (1 + 2%)
        "lot_spacing_atr": 1.0,          # add on dips: mark <= newest - 1.0*ATR14
        "sleeve_stop_pct": 15,           # HWM drawdown -> DISTRIBUTE_ONLY (de-risk)
        "derisk_steps": 3,
        "coverage_ratio": 1.0,
        "atr_period": 14,
    },
    # ---- sleeve: BTC_CP (spec 2.7, mirror, lower caps) ---------------------
    "btc_cp": {
        "enabled": True,
        "symbol": "BTCUSD",
        "lot_size_notional": ASK_NONE,   # [ASK] USD per lot
        "max_lots": 4,
        "sleeve_leverage_cap": 2,
        "lot_tp_pct": 2.0,
        "lot_spacing_atr": 1.0,
        "sleeve_stop_pct": 15,
        "derisk_steps": 3,
        "coverage_ratio": 1.0,
        "atr_period": 14,
    },
    # ---- sleeve: GOLD (spec 2.8) -------------------------------------------
    "gold": {
        "enabled": True,
        "symbols": ["XAUTUSD", "PAXGUSD"],
        "lot_size_notional": ASK_NONE,   # [ASK] USD per lot (shared sleeve)
        "max_lots": 6,                   # ONE shared bucket across XAUT + PAXG (ASSUMPTION)
        "sleeve_leverage_cap": 3,
        "lot_tp_pct": 1.0,
        "lot_spacing_atr": 0.8,
        "sleeve_stop_pct": 15,
        "derisk_steps": 3,
        "coverage_ratio": 1.0,
        "mode_switch_days": 3,           # regime flip must persist before a mode switch
        "atr_period": 14,
    },
    # ---- sleeve: STOCKS (spec 2.8) -----------------------------------------
    "stocks": {
        "enabled": True,
        "stock_symbols": ASK_NONE,       # [ASK] allow-list, e.g. ["NVDAXUSD", ...]
        "max_stock_symbols": 6,
        "lot_size_notional": ASK_NONE,   # [ASK] USD per lot
        "stock_max_lots": 5,             # per symbol (budget split across symbols)
        "stock_leverage_cap": 2,
        "lot_tp_pct": 2.0,               # software TP fallback for stock lots (ASSUMPTION)
        "lot_spacing_pct": 5,            # add per 5% below the newest lot entry
        "dd_lookback_days": 30,
        "dd_entry_pct": 8,
        "rsi_accumulate": 40,
        "rsi_distribute_floor": 60,
        "rebound_window_days": 5,
        "max_lots_sold_per_day": 1,
        "min_lot_profit_pct": 3,
        "stock_symbol_stop_pct": 25,     # down from avg cost -> DISTRIBUTE_ONLY
        "sleeve_stop_pct": 15,
        "derisk_steps": 3,
        "atr_period": 14,
        "spread_base_pct": 0.01,
        "spread_time_table": {           # widening multipliers by session (ASSUMPTION)
            "us_market_hours": 1.0,      # 13:30-20:00 UTC
            "us_market_closed": 3.0,
        },
        "us_market_hours_utc": ["13:30", "20:00"],
        "earnings_blackout_csv": None,   # optional earnings.csv path
        "earnings_blackout_days": 2,
    },
    # ---- allocation (spec 2.10) --------------------------------------------
    "allocation": {
        "strategy_capital_usd": ASK_NONE,   # [ASK] never exceeded by deployed capital
        "base_weights": {                   # [ASK]
            "eth_cc": ASK_NONE,
            "btc_cp": ASK_NONE,
            "gold": ASK_NONE,
            "stocks": ASK_NONE,
        },
        "cash_reserve_pct": 20,             # never allocated
        "w_min": 0.05,
        "w_max": 0.40,
        "regime_fit": {                     # sleeve fit by trend label (ASSUMPTION defaults)
            "eth_cc":  {"strong_down": 0.0, "down": 0.3, "neutral": 0.8, "up": 1.0, "strong_up": 1.0},
            "btc_cp":  {"strong_down": 1.0, "down": 1.0, "neutral": 0.8, "up": 0.3, "strong_up": 0.0},
            "gold":    {"strong_down": 0.5, "down": 0.7, "neutral": 1.0, "up": 0.7, "strong_up": 0.5},
            "stocks":  {"strong_down": 1.2, "down": 1.2, "neutral": 0.6, "up": 0.4, "strong_up": 0.2},
        },
    },
    # ---- global risk limits (spec 2.10, mandatory) --------------------------
    "risk": {
        "max_gross_notional_to_equity": 3.0,
        "max_margin_utilisation_pct": 50,
        "min_liq_distance_pct": 25,
        "max_daily_loss_pct": 3,
        "max_drawdown_pct": 12,
        "flatten_on_daily_loss": True,
        "flatten_on_drawdown": True,
        "high_vol_pctl": 85,                  # > this percentile -> halve new-lot size
    },
    # ---- regime (spec 2.9) ---------------------------------------------------
    "regime": {
        "ema_fast": 50,
        "ema_slow": 200,
        "slope_days": 10,
        "vol_atr_period": 14,
        "vol_lookback": 90,
        "dd_lookback": 90,
        "neutral_band": 0.5,                  # |score| <= band -> neutral
        "strong_band": 2.0,                   # |score| >= band -> strong_*
        "component_cap": 2.0,                 # per-component saturation (ASSUMPTION)
        "eth_cc_map": {"strong_down": "PAUSED", "down": "ACCUMULATE_ONLY",
                        "neutral": "ACTIVE", "up": "ACTIVE", "strong_up": "ACTIVE"},
        "btc_cp_map": {"strong_down": "ACTIVE", "down": "ACTIVE",
                        "neutral": "ACTIVE", "up": "DISTRIBUTE_ONLY",
                        "strong_up": "PAUSED"},
        "gold_cc_map": {"strong_down": "PAUSED", "down": "PAUSED",
                         "neutral": "ACTIVE", "up": "ACTIVE", "strong_up": "ACTIVE"},
        "gold_cp_map": {"strong_down": "ACTIVE", "down": "ACTIVE",
                         "neutral": "ACTIVE", "up": "PAUSED", "strong_up": "PAUSED"},
    },
}


def merge_defaults(overrides: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Deep-merge DEFAULTS with the `all_in_one:` section of config.yaml."""
    import copy

    merged = copy.deepcopy(DEFAULTS)
    if not overrides:
        return merged

    def _merge(base: dict[str, Any], extra: dict[str, Any]) -> None:
        for k, v in extra.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                _merge(base[k], v)
            else:
                base[k] = v

    _merge(merged, overrides)
    return merged


def get(cfg: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """Read a dotted path from the merged all_in_one config tree."""
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node
