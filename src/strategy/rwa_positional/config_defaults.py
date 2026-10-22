"""Default configuration for `rwa_positional` (prompt A, Part 2).

Every number in the spec is a config key. The strategy reads the sub-tree
`config["rwa_positional"]` and merges it over these DEFAULTS (user wins).

Key the package docstring requirement (loader in core/config.py): the [ASK]
keys that must appear in config.yaml under `rwa_positional:` (values may stay
null; live trading is blocked while null):

    rwa_positional:
      recommended_margin: null    # leader's published Recommended Margin
      min_turnover_usd: null      # 24h turnover liquidity floor (USD)
      leverage: null              # campaign effective leverage
      margin_mode: null           # cross | isolated

Every other key has a concrete default below, so config.yaml may omit it.
"""
from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    # ---- universe (2.2) ---------------------------------------------------
    "symbols": ["XAUTUSD", "PAXGUSD", "SLVONUSD"],
    "energy_scan": True,           # scan /products for energy perps
    "gold_bucket_symbols": ["XAUTUSD", "PAXGUSD"],
    "max_campaigns_per_bucket": 1,

    # ---- data / timeframes (2.3) ------------------------------------------
    "htf": "4H",
    "levels_tf": "1D",
    "zone_tf": "1H",
    "trigger_tf": "15m",
    "warmup_bars": {"4H": 500, "1D": 60, "1H": 500, "15m": 500},
    "atr_period": 14,
    "pivot_left_1h": 3,
    "pivot_right_1h": 3,
    "pivot_left_4h": 2,
    "pivot_right_4h": 2,
    "ema_fast": 50,
    "ema_slow": 200,
    "volume_sma_len": 20,

    # ---- structure (2.4) ---------------------------------------------------
    "use_ema_tiebreak": True,

    # ---- levels / zones (2.5) ----------------------------------------------
    "level_swing_lookback": 10,      # last N 4H swing H/L as candidates
    "equal_cluster_atr_mult": 0.15,  # equal-high/low cluster tolerance
    "zone_atr_mult": 0.25,           # zone = level ± 0.25 × ATR(1H)
    "min_zone_score": 2,
    "round_numbers": {"enabled": False, "tolerance_pct": 0.5, "score": 1},

    # ---- sweep trigger (2.6) ------------------------------------------------
    "sweep_min_atr": 0.10,
    "reclaim_bars": 3,
    "sweep_volume_mult": 1.2,
    "sweep_wick_frac": 0.5,
    "allow_countertrend": False,
    "max_spread_pct": 0.10,
    "min_turnover_usd": None,        # [ASK]
    "price_band_buffer_pct": 1.0,    # skip when within 1% of band edge
    "cooldown_bars": 6,              # 1H bars after a campaign closes

    # ---- campaign ladder (2.7) ----------------------------------------------
    "rungs": 4,
    "rung_weights": [0.25, 0.25, 0.25, 0.25],
    "martingale_multiplier": 1.0,    # warn when > 1.0 (NOT martingale by default)
    "ladder_atr_mult": [0.6, 1.2, 1.8],
    "ladder_mode": "against",        # against | with
    "entry_timeout_s": 20,
    "cross_ticks": 2,                # IOC fallback: best ± 2 ticks
    "allow_market": False,
    "rung_ttl_hours": 72,

    # ---- invalidation / risk (2.8) -------------------------------------------
    "campaign_risk_pct": 1.0,
    "invalidation_atr_mult": 0.5,
    "stop_trigger_method": "mark_price",

    # ---- profit booking (2.9) -------------------------------------------------
    "profit_target_pct_of_margin": 20.0,
    "tp_close_fraction": 1.0,
    "trail_atr_mult": None,          # trailing disabled by default
    "max_campaign_days": 14,

    # ---- sizing / leverage (2.10) ---------------------------------------------
    "campaign_margin_pct": 15.0,
    "leverage": None,                # [ASK]
    "max_leverage": 10,
    "default_leverage_fallback": 5.0,  # used only while [ASK] leverage is null (risk arithmetic needs SOME number; see README_note)
    "recommended_margin": None,      # [ASK]
    "margin_mode": None,             # [ASK] cross | isolated
    "isolated_auto_topup": False,

    # ---- portfolio limits (2.11) ------------------------------------------------
    "max_concurrent_campaigns": 3,
    "max_total_margin_pct": 40.0,
    "max_daily_loss_pct": 3.0,
    "max_drawdown_pct": 10.0,
    "liquidation_atr_mult_4h": 2.0,

    # ---- misc --------------------------------------------------------------------
    "algo_name": "rwa_positional",
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def merged_config(user: dict[str, Any] | None) -> dict[str, Any]:
    return deep_merge(DEFAULTS, user or {})
