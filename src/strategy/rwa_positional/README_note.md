# README note — `strategy/rwa_positional`

Honesty note (spec 3.9): the strategy spec (prompt A Part 2) is INFERRED from a
public marketing description of a Delta Exchange India leader, not from the
leader's real rules. This package does NOT claim to reproduce the leader's
results. This bot is not financial advice; leveraged derivatives can lose more
than your deposit; default to testnet and small size.

## 1. Config keys read (dotted paths, all under `rwa_positional.*`)

### [ASK] keys (must exist in coordination-owned `config.yaml`; `null` blocks live)
| key | default | meaning |
|---|---|---|
| `recommended_margin` | `null` [ASK] | leader's "Recommended Margin" section (not captured; numeric placeholder |
| `min_turnover_usd` | `null` [ASK] | 24h USD turnover floor from /tickers — null disables the filter |
| `leverage` | `null` [ASK] | campaign effective leverage (number, or per-symbol dict); hard-capped by `max_leverage`; **while null the strategy falls back to `default_leverage_fallback` (5.0) so risk arithmetic has SOME number — this is an assumption, surfaced loudly here |
| `margin_mode` | `null` [ASK] | `cross` or `isolated`; null blocks live via core.config |

### Numeric/structural defaults (see `config_defaults.DEFAULTS` for every one)
`symbols` (XAUTUSD, PAXGUSD, SLVONUSD) · `energy_scan` true · `max_campaigns_per_bucket` 1 ·
`htf` 4H · `zone_tf` 1H · `trigger_tf` 15m · `levels_tf` 1D · `atr_period` 14 ·
`pivot_left/right_4h` 2 · `pivot_left/right_1h` 3 · `use_ema_tiebreak` true ·
`level_swing_lookback` 10 · `equal_cluster_atr_mult` 0.15 · `zone_atr_mult` 0.25 ·
`min_zone_score` 2 · `round_numbers.enabled` false ·
`sweep_min_atr` 0.10 · `reclaim_bars` 3 · `sweep_volume_mult` 1.2 · `sweep_wick_frac` 0.5 ·
`allow_countertrend` false · `max_spread_pct` 0.10 · `price_band_buffer_pct` 1.0 ·
`cooldown_bars` 6 · `rungs` 4 · `rung_weights` [0.25×4] · `martingale_multiplier` 1.0 (WARN > 1.0) ·
`ladder_atr_mult` [0.6, 1.2, 1.8] · `ladder_mode` against · `entry_timeout_s` 20 ·
`cross_ticks` 2 · `allow_market` false · `rung_ttl_hours` 72 ·
`campaign_risk_pct` 1.0 · `invalidation_atr_mult` 0.5 · `stop_trigger_method` mark_price ·
`profit_target_pct_of_margin` 20 · `tp_close_fraction` 1.0 · `trail_atr_mult` null ·
`max_campaign_days` 14 · `campaign_margin_pct` 15 · `max_leverage` 10 ·
`max_concurrent_campaigns` 3 · `max_total_margin_pct` 40 · `max_daily_loss_pct` 3 ·
`max_drawdown_pct` 10 · `liquidation_atr_mult_4h` 2 · `isolated_auto_topup` false.

## 2. Assumptions / interpretations chosen (be suspicious of each)

1. **Sweep confirmation.** A sweep bar = wick beyond the zone edge by ≥
   `sweep_min_atr × ATR(1H)` AND (volume ≥ 1.2 × SMA20 of the *preceding* 20
   15m bars OR the wick ≥ 50% of the bar range). Confirmation = a later 15m
   CLOSE back beyond the zone's mid level within `reclaim_bars`. The sweep bar
   itself closing beyond the level is allowed (we only require the later
   reclaim close). The detector fires the FRESHEST qualifying sweep only.
2. **Direction filter in range.** "range → longs allowed when zone ≤ range
   mid" is applied for all range sub-flavours (`range_long`/`range_short`
   EMA-tiebreak still respects the mid-rule; the EMA tiebreak only selects
   which side to ***prefer*** when the mid is undefined).
3. **Equal-high/low clusters.** Cluster tolerance uses ATR(1H) as the spec
   says; cluster members must be 4H swing points (any kind per 2.5 — highs and
   lows are clustered together by proximity, judged cheap and conservative).
4. **Prev week.** "previous week" = the last 7 COMPLETED 1D candles before
   today (candles[-9:-2]); previous day = candles[-2].
5. **≈20% of margin.** PnL target expressed EXACTLY as
   `profit_target_pct_of_margin/100 / effective_leverage` price move AND as a
   mark-tick check `pnl ≥ pct% × (notional / leverage)`. Fees/funding are NOT
   modelled inside the software TP check (the backtester must model them;
   spec 2.14).
6. **Risk-cap sizing.** Rung margins are scaled by
   `min(1, risk_cap / full_ladder_risk)` where per-rung risk =
   `margin × leverage × |rung_price − SL| / rung_price`. If rung 1's scaled
   margin yields floor(0) contracts (or the budget is zero), the signal is
   SKIPPED (spec: "if rung 1 alone would breach it, skip the signal").
7. **Rung prices beyond invalidation are DROPPED** (never clamped) — a rung
   sitting at/beyond the SL would be instant stop-out noise.
8. **Entry timeout.** Rung 1 = post-only limit at best bid (long). After
   `entry_timeout_s`, ONE IOC cross fallback at best±2×tick; if that also
   fails within another timeout → abort unless `allow_market: true` (never a
   blind market order, spec 2.7).
9. **State EXITING from exchange brackets.** When the exchange-side SL/TP
   fires (fill arrives via on_fill), the state machine still walks
   `…MANAGING → EXITING → COOLDOWN` so the persisted state path is uniform.
10. **Resting rungs after restart are NOT re-placed** by `_reload_open_campaigns`
    (they are marked `placed` from meta); reconciliation with
    `exec.open_orders()` is a TODO(VERIFY) — restart may start with missing
    ladder adds until the timer's rung retry code path re-places them
    (`_place_resting_rungs` runs on every entry fill, so a fresh restart is
    covered only if more fills arrive).
11. **Campaign margin bookkeeping** is software-side: notional/leverage from
    our own fills. The exchange's position `margin` field is read where
    available (TODO(VERIFY) exact positions-endpoint field name).
12. **Energy discovery** matches raw text keywords (oil/gas/brent/wti/crude/
    natgas/…) over description/tags/short_description — a heuristic; verify
    against live `/products` listings.
13. **One net position per product**: rungs/lots/campaigns exist only in the
    SQLite ledger (spec 1.6); the exchange sees one netted position + a single
    re-centred bracket.
14. **In-memory per-SYMBOL state (zones/activation)** is persisted as a whole
    FeatureSnapshot with each state transition; arena-level rebuild from DB is
    limited to campaigns/lots (the feature snapshot is advisory on restart).

## 3. TODO(VERIFY) markers in code
- `strategy.py::_reload_open_campaigns` — resting rung re-placement after restart.
- `strategy.py::_send_bracket` — Delta PUT /orders/bracket payload shape must be
  confirmed against the live docs (host's DeltaPort maps the intent; verify
  `bracket_stop_loss_price` vs `bracket_stop_loss_limit_price` requirements).
- Per-product order-leverage endpoint path `(VERIFY)` in spec 1.7 — the port
  abstraction hides it; confirm when wiring DeltaPort.
