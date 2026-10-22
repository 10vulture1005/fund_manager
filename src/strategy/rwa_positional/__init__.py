"""RWA Metals & Energy — Positional Trading strategy (prompt A, Part 2).

Exports: `RwaPositionalStrategy` (the StrategyPort implementation),
`SweepDetector` (liquidity-sweep trigger) and `Levels` (zone builder).

Package docstring note — the [ASK] config keys this strategy needs from the
coordinator-owned `config.yaml` (under `rwa_positional:`, values may stay
null; core.config.missing_ask_keys/assert_live_ready block live trading while
they are null):

    rwa_positional:
      recommended_margin: null   # leader's "Recommended Margin" reference
      min_turnover_usd: null     # 24h turnover floor (USD) for signal validity
      leverage: null             # campaign effective (order) leverage
      margin_mode: null          # cross | isolated

Every other parameter has a concrete default in config_defaults.DEFAULTS —
see README_note.md in this package for the full key list and the choices made.
"""
from __future__ import annotations

from strategy.rwa_positional.config_defaults import DEFAULTS, merged_config
from strategy.rwa_positional.levels import Level, Levels, Zone
from strategy.rwa_positional.sweep import SweepDetector, SweepSignal, detect_sweep
from strategy.rwa_positional.strategy import RwaPositionalStrategy

__all__ = [
    "DEFAULTS",
    "Level",
    "Levels",
    "RwaPositionalStrategy",
    "SweepDetector",
    "SweepSignal",
    "Zone",
    "detect_sweep",
    "merged_config",
]
