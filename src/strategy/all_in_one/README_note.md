# STRATEGY B — `all_in_one` — config keys, defaults and assumptions

One-pager of every config key the strategy reads (dotted paths under the
config.yaml `all_in_one:` subtree), its default, and the honest
assumptions/ambiguities (spec 3.9). The strategy code refuses nothing — `[ASK]`
values must be filled by the human; `core.config.missing_ask_keys("all_in_one")`
blocks live trading while they are null.

## Config keys (dotted path — default)

Format: `path (default)` — everything is merged from `defaults.DEFAULTS`
deep-merged with `config.yaml`'s `all_in_one:` section (`merge_defaults`).

### Global
- `lifo_strict` (true) — spec 2.4: only the newest open lot may close
- `margin_mode_required` ("cross") — spec 2.3 (bot REACTS to the mode read by the infra layer)
- `flatten_on_kill` (true), `flatten_safety_loop` (50)
- `options_cycle_minutes` (15 — informational; cadence lives in on_timer)
- `buffer_candles_1d` (400), `buffer_candles_4h` (200)
- `opt_min_dte` (5), `opt_max_dte` (10), `opt_target_delta` (0.20),
  `opt_delta_band` ([0.12, 0.30]), `opt_max_spread_pct` (0.08),
  `opt_min_oi` (100.0 — no default in the spec; ASSUMPTION),
  `opt_reprice_s` (10), `opt_max_reprices` (6),
  `opt_buyback_capture_pct` (80), `opt_roll_delta` (0.45),
  `iv_premium` (1.1), `hedge_style` ("standard")

### Sleeve `eth_cc.`
`symbol` (ETHUSD), `lot_size_notional` ([ASK] null), `max_lots` (5),
`sleeve_leverage_cap` (3), `lot_tp_pct` (2.0), `lot_spacing_atr` (1.0),
`sleeve_stop_pct` (15), `derisk_steps` (3), `coverage_ratio` (1.0),
`atr_period` (14), `enabled` (true)
- NOTE: the actual per-lot size is `allocation.budget / max_lots`
  (spec 2.6); `lot_size_notional` is read first, and if set it is used as
  a sanity cap on the per-lot budget. TODO(VERIFY) intent.

### Sleeve `btc_cp.`
`symbol` (BTCUSD), `lot_size_notional` ([ASK] null), `max_lots` (4),
`sleeve_leverage_cap` (2), `lot_tp_pct` (2.0), `lot_spacing_atr` (1.0),
`sleeve_stop_pct` (15), `derisk_steps` (3), `coverage_ratio` (1.0),
`atr_period` (14), `enabled` (true)

### Sleeve `gold.`
`symbols` ([XAUTUSD, PAXGUSD]), `lot_size_notional` ([ASK] null),
`max_lots` (6 — ONE shared bucket; no number in the spec; ASSUMPTION),
`sleeve_leverage_cap` (3), `lot_tp_pct` (1.0), `lot_spacing_atr` (0.8),
`sleeve_stop_pct` (15), `derisk_steps` (3), `coverage_ratio` (1.0),
`mode_switch_days` (3), `atr_period` (14), `enabled` (true)

### Sleeve `stocks.`
`stock_symbols` ([ASK] null), `max_stock_symbols` (6),
`lot_size_notional` ([ASK] null), `stock_max_lots` (5),
`stock_leverage_cap` (2), `lot_tp_pct` (2.0 — software TP fallback; ASSUMPTION),
`lot_spacing_pct` (5), `dd_lookback_days` (30), `dd_entry_pct` (8),
`rsi_accumulate` (40), `rsi_distribute_floor` (60), `rebound_window_days` (5),
`max_lots_sold_per_day` (1), `min_lot_profit_pct` (3),
`stock_symbol_stop_pct` (25), `sleeve_stop_pct` (15), `derisk_steps` (3),
`atr_period` (14), `spread_base_pct` (0.01),
`spread_time_table` ({us_market_hours: 1.0, us_market_closed: 3.0} —
multipliers on spread_base_pct; ASSUMPTION table),
`us_market_hours_utc` (["13:30","20:00"]), `earnings_blackout_csv` (null,
optional), `earnings_blackout_days` (2), `enabled` (true)

### `allocation.`
`strategy_capital_usd` ([ASK] null), `base_weights.eth_cc` / `.btc_cp` /
`.gold` / `.stocks` ([ASK] null each), `cash_reserve_pct` (20),
`w_min` (0.05), `w_max` (0.40), `regime_fit` (per-sleeve trend table — ASSUMPTION
defaults in defaults.py)

### `risk.`
`max_gross_notional_to_equity` (3), `max_margin_utilisation_pct` (50),
`min_liq_distance_pct` (25), `max_daily_loss_pct` (3), `max_drawdown_pct` (12),
`flatten_on_daily_loss` (true — informational), `flatten_on_drawdown` (true),
`high_vol_pctl` (85)

### `regime.`
`ema_fast` (50), `ema_slow` (200), `slope_days` (10), `vol_atr_period` (14),
`vol_lookback` (90), `dd_lookback` (90), `neutral_band` (0.5),
`strong_band` (2.0), `eth_cc_map` / `btc_cp_map` / `gold_cc_map` / `gold_cp_map`
(spec 2.9 defaults).

## Assumptions / ambiguities (honesty, spec 3.9)

1. **"LIFO position management"** means: positions are built as discrete
   LOTS; the MOST RECENTLY OPENED lot of a (sleeve, symbol) is the first one
   closed. Strict mode: older lots are never closed while a newer one is
   open; the exchange only ever sees one NET position per product, lot
   identity lives in the SQLite ledger only.
2. **A "lot"** is one entry fill of `budget/max_lots` notional (or an add on
   the spacing trigger). `contrats = floor(notional / (price * contract_value))`,
   integer contracts.
3. **Covered call / covered put** = standard definition (short options
   against perp lots), not necessarily the leader's private structure
   (`hedge_style` config captures the alternative).
4. **Options availability**: options on Delta exist for BTC/ETH only —
   XAUT/PAXG sleeves run `hedge_mode none` (perp ladder + LIFO only),
   detected live via the products/tickers, never assumed.
5. **Regime scoring**: spec says "sign and normalised distance ... plus
   EMA50 slope". The concrete formula: score = 0.5*comp(d_fast) +
   0.25*comp(d_slow) + 0.25*comp(d_slope) with comp = sign(x)*min(|x|,1),
   distances in ATR(14) units; neutral_band 0.5, strong_band 2.0 — both
   configurable keys, chosen by me.
6. **BTC_CP up-regime**: spec says "DISTRIBUTE_ONLY/PAUSED" without saying
   which for `up`; I map up -> DISTRIBUTE_ONLY (harvest shorts into
   strength), strong_up -> PAUSED.
7. **GOLD max_lots** ("shared"): no number was given; 6 total (3 per symbol)
   is my default.
8. **GOLD neutral regime**: CC is chosen in neutral tie-break (spec lists
   CC under neutral/up, CP under neutral/down).
9. **Stock distribute**: "sell limit orders near the touch" is implemented
   as a reduce-only limit at the current best bid.
10. **Cash reserve (20%)** is never allocated; deployed capital is capped by
    `strategy_capital_usd` [ASK] even if account equity is larger.
11. **Cross margin**: the bot does NOT read the margin mode itself — the
    executor/infra layer reports it hourly (`portfolio.set_margin_mode`).
    New lots and option sales are stopped when the flag is not cross.
12. **Options cash settlement at DTE<=1** is assumed — TODO(VERIFY) in
    Delta's option product docs before live use.
13. **Sleeve stop high-water**: "equity attributable to the sleeve" is
    estimated as the sleeve's realized lot P&L; the HWM is the max seen.
    Perfect attribution would need funding/fees per sleeve — TODO(VERIFY).
14. **Stock perp sessions**: spread widening table and US market hours
    (13:30–20:00 UTC) are my defaults, verify against the venue's real
    session behaviour.
15. **Funding costs** accrue on the perp legs and are not per-lot attributed
    in the ledger (TODO(VERIFY) wallet/transactions attribution).
16. Not financial advice; this strategy is INFERRED from a public marketing
    description — it does not reproduce the leader's results. Testnet and
    small size first.
