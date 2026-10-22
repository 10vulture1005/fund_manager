# PROMPT A — "RWA Metals & Energy — Positional Trading" bot for Delta Exchange India

## ROLE
You are a senior quantitative developer. Build a production-grade, restart-safe trading bot in Python that recreates the strategy described below on Delta Exchange India, plus a backtester for it. Follow Part 1 (API reference), Part 2 (strategy), Part 3 (engineering). Ask me nothing until you have read everything; then list your assumptions in `ASSUMPTIONS.md` and build. Any parameter tagged [ASK] must be a config key that defaults to `null` and blocks live trading until I fill it.

# PART 1 — DELTA EXCHANGE INDIA API REFERENCE (use as ground truth)

Source: https://docs.delta.exchange . Before coding, open and re-read these sections of the docs: Authentication, Rate Limits, Products, Orders, Positions, Account (Change margin mode, trading preferences), Wallet, Historical OHLC Candles, Heartbeat/Deadman Switch, Websocket Feed (Authenticating, Public Channels, Private Channels), Place order errors. If anything in this summary conflicts with the live docs, the live docs win. Items marked (VERIFY) are things I could not confirm word-for-word; confirm them in the docs before relying on them.

## 1.1 Environments
- Production REST: https://api.india.delta.exchange
- Testnet / demo REST: https://cdn-ind.testnet.deltaex.org  (keys created on demo.delta.exchange work ONLY here; a demo key sent to prod returns `invalid_api_key`)
- NEVER use https://api.delta.exchange — that is Delta Global, not India.
- Config switch `env: testnet | prod`. Default is testnet. Prod must additionally require the CLI flag `--i-understand-this-is-live`.

## 1.2 Authentication (every private request)
Headers: `api-key`, `signature`, `timestamp`, `User-Agent` (mandatory — missing UA gets a 4xx / "Request blocked by CDN"), `Content-Type: application/json`.
`signature = hex( HMAC_SHA256(api_secret, method + timestamp + path + query_string + body) )`
- method upper-case; `timestamp` = Unix SECONDS as a string, identical to the header value
- `path` e.g. `/v2/orders`; `query_string` INCLUDES the leading `?` (e.g. `?product_id=1&state=open`) or is an empty string
- `body` = the exact raw JSON string that is sent (serialize ONCE with compact separators, sign that string, send that same string; never re-serialize)
- A signature is valid for only 5 seconds after generation: sign per request, keep the host clock NTP-synced, on `SignatureExpired` re-sign and retry once.
- The key needs Trading permission for orders / positions / margin / leverage, and trading keys require IP whitelisting (IPv4 and IPv6 are different entries).
- Handle explicitly: `invalid_api_key`, `SignatureExpired`, `UnauthorizedApiAccess`, `ip_not_whitelisted_for_api_key` (log the IP in the error), `Signature Mismatch`, 403 "Request blocked by CDN".
- NOTE: the sample signature printed in the docs does NOT reproduce with the printed secret, so do not use it as a unit-test vector. Test the signer by (a) checking it against an independent HMAC computation in the test and (b) one real authenticated GET on testnet.

## 1.3 Conventions
- Big decimals (prices, trail amounts, margins) are STRINGS in requests and responses; `size`, `product_id` are integers. Send prices as strings.
- Response timestamps are microseconds. Request `start_time`/`end_time` filters on orders are microseconds too; candle APIs use seconds (VERIFY).
- Envelope: success `{ "success": true, "result": ..., "meta": { "after": ..., "before": ... } }`; error `{ "success": false, "error": { "code": "...", "context": {...} } }` (e.g. `insufficient_margin` with `additional_margin_required`).
- Cursor pagination (`after`, `before`, `page_size`) on: /v2/products, /v2/orders, /v2/orders/history, /v2/fills, /v2/wallet/transactions. Cursors change on every page.

## 1.4 Rate limits
- Quota: 20,000 weight per fixed 5-minute window per user; HTTP 429 when exceeded, header `X-RATE-LIMIT-RESET` = milliseconds until you may call again.
- Weights: 3 = products, orderbook, tickers, open orders, open positions, balances, OHLC candles; 5 = place / edit / delete order, add position margin; 10 = order history, fills, transaction logs; 25 = batch order APIs; everything else 1.
- Per-product matching-engine limit: 500 operations/second/product (a batch of 50 orders = 50 operations). Cancels are exempt.
- Build a client-side limiter that stays under ~60% of the quota, honours X-RATE-LIMIT-RESET, and prefers WebSocket streams over polling.

## 1.5 REST endpoints you will use (base path /v2)
Public: GET /products (filters: contract_types, states, page_size, after), GET /products/{symbol}, GET /tickers (filters: contract_types, underlying_asset_symbols, expiry_date DD-MM-YYYY), GET /tickers/{symbol} (up to 10 comma-separated symbols), GET /l2orderbook/{symbol} (VERIFY path), GET /history/candles (symbol, resolution, start, end) (VERIFY params), GET /assets.
Option chain = GET /tickers?contract_types=call_options,put_options&underlying_asset_symbols=BTC&expiry_date=DD-MM-YYYY (tickers include `greeks`, `quotes.best_bid/best_ask/bid_iv/ask_iv`, `mark_price`, `mark_vol`, `oi`, `strike_price`, `spot_price`, `price_band`).
Private: POST /orders, PUT /orders, DELETE /orders (body: id or client_order_id + product_id), GET /orders (states=open,pending; product_ids ≤ 10), GET /orders/{id}, GET order by client_oid (VERIFY path), POST/PUT /orders/bracket, DELETE /orders/all, POST/PUT/DELETE /orders/batch (same product, max 50, limit orders only, gtc only), GET /positions/margined and GET /positions (VERIFY), POST add/remove position margin, POST auto top-up, POST close-all positions, GET/POST order leverage per product (VERIFY: /products/{product_id}/orders/leverage), GET /wallet/balances, GET /orders/history, GET /fills, Account: trading preferences, change margin mode, subaccounts, rate-limit quota. Heartbeat endpoints for the Deadman Switch.

## 1.6 Product discovery and sizing rules
- NEVER hard-code product_ids. On startup call GET /products?states=live (paginate fully), index by `symbol`, and resolve every configured symbol to `id`, `tick_size`, `contract_value`, `contract_unit_currency`, `contract_type`, `default_leverage`, `max_leverage_notional`, `position_size_limit`, `maker_commission_rate`, `taker_commission_rate`, `trading_status`, `product_specs.only_reduce_only_orders_allowed`, `product_specs.tags`, `price_band`. If a configured symbol is missing or not `operational`, skip it, log loudly, and keep running the rest.
- `size` is an integer number of CONTRACTS. contracts = floor(target_notional / (price × contract_value)). If it floors to 0, skip and log. Round limit/stop prices to `tick_size` (string).
- Known live RWA perps on Delta India (verify via /products, categories are not guaranteed): XAUTUSD, PAXGUSD (commodity perps, up to 100x), SLVONUSD (silver), and US-equity "stock perps" with symbols like NVDAXUSD, TSLAXUSD, AAPLXUSD, METAXUSD, AMZNXUSD, GOOGLXUSD (max ~25x). Options exist for BTC and ETH; do not assume options exist for any RWA — detect them via GET /tickers?contract_types=call_options,put_options&underlying_asset_symbols=<SYM>.
- Delta holds ONE net position per product (no separate long and short legs on the same contract) (VERIFY). Any "lot", "ladder" or "campaign" concept therefore lives in YOUR database, not on the exchange.

## 1.7 Order rules
- Fields: product_id (or product_symbol), size, side (buy|sell), order_type (limit_order|market_order), limit_price (string), time_in_force (gtc|ioc), post_only, reduce_only, client_order_id, stop_order_type (stop_loss_order|take_profit_order), stop_price, stop_trigger_method (mark_price|last_traded_price|spot_price), trail_amount, bracket_stop_loss_price / bracket_stop_loss_limit_price / bracket_take_profit_price / bracket_take_profit_limit_price / bracket_trail_amount / bracket_stop_trigger_method, mmp.
- ALWAYS set a unique `client_order_id` shaped `{algo}-{symbol}-{purpose}-{uuid8}`; on a timeout or ambiguous response, look the order up by client_order_id before retrying (idempotency). Never blind-retry a POST /orders.
- EVERY exit order is `reduce_only: true`. Passive entries/adds use `post_only: true`.
- Use stop_trigger_method `mark_price` for stop-losses (avoids wick triggers on thin books).
- A bracket order (TP + SL) needs no size and closes the whole position; only ONE bracket per open position. After any change to the average entry price (each scale-in fill), PUT /orders/bracket to re-centre TP/SL.
- Respect product `trading_status`: `disrupted_cancel_only` and `disrupted_post_only` restrict order types; also respect `only_reduce_only_orders_allowed`.
- Handle placement errors from "Place order errors" in the docs (insufficient margin, price band breach, size limit, etc.) with explicit, logged handling — never swallow them.
- Reconcile with the private WebSocket channels (orders, positions, margins, user trades) and with REST on startup and after every reconnect.

## 1.8 WebSocket (follow the docs' "Websocket Feed" section exactly)
- Public channels needed: candlesticks, ticker (or ob_l1 for best bid/ask), mark_price, funding_rate, product_updates, system_status.
- Private channels needed: positions, orders, margins, user trades (check the difference between UserTrades and v2/user_trades in the docs). Authenticate with the documented method (VERIFY exact URL and auth message; the India socket host differs from Delta Global).
- Detect dropped connections with the documented heartbeat; reconnect with exponential back-off + jitter; after reconnect, resubscribe, then run a full REST reconciliation before the strategy is allowed to trade again.

## 1.9 Deadman switch
Delta provides a heartbeat-based Deadman Switch (Heartbeat Management endpoints). Implement it: the bot registers a heartbeat and acknowledges it on a timer; if the bot dies the exchange cancels open orders. Make it configurable and default ON in prod. Read the docs section "Deadman Switch → Implementation Guidelines / Error Codes" first.

---

# PART 2 — STRATEGY SPEC: `rwa_positional`

## 2.1 What is KNOWN (the leader's public description, paraphrased)
- Positional strategy on RWA (real-world-asset, tokenized) markets, mainly metals and energy.
- Trades both LONG and SHORT.
- Decisions rest on market structure, liquidity conditions and key price levels.
- It watches for LIQUIDITY SWEEPS around those levels to find entries.
- Positions may be SCALED IN progressively as price reaches predefined levels, building exposure around high-conviction zones instead of using one entry.
- Structured position management and risk control.
- Typical profit-booking objective: about 20% of USED MARGIN.
- The leader page also has a "Recommended Margin" section that I have not captured → [ASK] `recommended_margin`.
Everything below is my best-effort concrete interpretation. Make every number a config value.

## 2.2 Universe (config, verified at startup against /products)
Default symbols: `XAUTUSD`, `PAXGUSD`, `SLVONUSD`. Also scan live perpetuals for any energy/oil/gas product (match on `description`, `product_specs.tags`, `short_description`); if none is live, run metals only and write that to the startup log. Each symbol is an independent "book" with at most one open campaign at a time (Delta nets positions per product). Gold tokens XAUT and PAXG are highly correlated → share one risk bucket (`max_campaigns_per_bucket: 1`).

## 2.3 Data and timeframes
Warm up from historical candles, then stream candlesticks. HTF = 4H (bias) and 1D (levels); LTF = 1H (zones, ATR) and 15m (trigger). Use closed candles only.
Indicators: ATR(14) on 1H and 4H; EMA(50) and EMA(200) on 4H; volume SMA(20) on 15m; swing pivots = fractal with `left=right=3` on 1H and `left=right=2` on 4H.

## 2.4 Market-structure bias (4H)
- Bullish: last two confirmed swing highs and swing lows are rising (HH+HL). Bearish: LH+LL. Otherwise `range`.
- BOS = 4H close beyond the last swing in the trend direction; CHoCH = 4H close beyond the last opposite swing → bias flips.
- Tiebreak in `range`: price above EMA200(4H) leans long, below leans short (config `use_ema_tiebreak`).

## 2.5 Key levels and zones
Level candidates: previous-day high/low, previous-week high/low, last 10 swing highs/lows on 4H, equal-high / equal-low clusters (≥2 swing points within `0.15 × ATR(1H)` of each other), round numbers (config, off by default).
Score each level +1 per independent confluence (HTF swing, prev day/week, equal cluster, ≥2 prior touches). A "high-conviction zone" is `level ± 0.25 × ATR(1H)` with score ≥ `min_zone_score` (default 2).

## 2.6 Liquidity-sweep trigger (evaluated on each closed 15m candle)
- SELL-SIDE sweep → LONG setup: a candle wicks below the zone's low by ≥ `sweep_min_atr (0.10) × ATR(1H)` AND price closes back above the level within `reclaim_bars (3)` candles AND (volume ≥ `1.2 × SMA20` OR lower wick ≥ 50% of the candle range).
- BUY-SIDE sweep → SHORT setup: mirror image above a zone's high.
- Direction filter: take longs when 4H bias is bullish, or range with the zone at/below range mid; shorts mirrored. Counter-trend sweeps only if `allow_countertrend: true` AND zone score ≥ 3 (default false).
- Liquidity filter (from /tickers `quotes`): spread = (best_ask − best_bid)/mid ≤ `max_spread_pct (0.10)`; 24h turnover ≥ `min_turnover_usd` [ASK]; product `trading_status == operational`; skip if price is within 1% of the product's `price_band` limits.
- Cooldown: no new campaign on the symbol for `cooldown_bars (6)` 1H bars after a campaign closes.

## 2.7 Campaign = scale-in ladder
A campaign is one directional position built in up to `rungs` (default 4) pieces.
- Rung 1 (entry): on signal, post-only limit at the best bid/ask; if not filled within `entry_timeout_s (20)`, cross with a limit at best ± 2 ticks (IOC fallback); never a pure market order unless `allow_market: true`.
- Rungs 2..N (predefined levels): after rung 1 fills, place resting post-only limits at `E1 ∓ k × ATR(1H)` for k in `ladder_atr_mult = [0.6, 1.2, 1.8]` (adverse direction, i.e. average-into-the-zone). Hard rule: no rung may sit beyond the invalidation price (2.8). Config `ladder_mode: against | with` (`with` = pyramid on break-and-retest in trade direction; default `against`).
- Rung weights: `rung_weights = [0.25, 0.25, 0.25, 0.25]` of the campaign's margin budget (equal, NOT martingale; a `martingale_multiplier` key exists but defaults to 1.0 and logs a warning above 1.0).
- Cancel unfilled rungs when: campaign exits, invalidation triggers, campaign age > `rung_ttl_hours (72)`, or margin utilisation cap is hit.
- After EVERY rung fill: recompute average entry, total contracts, margin used, PUT /orders/bracket to re-centre TP and SL.

## 2.8 Invalidation / stop (risk control)
- Invalidation price: beyond the sweep wick extreme by `0.5 × ATR(1H)` (below the sweep low for longs, above the sweep high for shorts).
- Exchange-side SL via bracket order, `stop_trigger_method: mark_price`.
- Campaign risk cap: if all rungs fill and SL hits, loss ≤ `campaign_risk_pct (1.0%)` of account equity. Size the rungs so this holds (if the ladder cannot satisfy it, shrink rung sizes; if rung 1 alone would breach it, skip the signal).
- Structure exit: a 4H close beyond the zone together with a CHoCH against the position → close the whole campaign (reduce_only market/aggressive limit) and cancel resting rungs.

## 2.9 Profit booking (the "≈20% of used margin" rule)
- Used margin = campaign notional / effective leverage (cross or isolated margin, as configured; read the position's margin from the positions endpoint where available).
- Target: close the campaign when unrealised PnL ≥ `profit_target_pct_of_margin (20)`% of used margin. Equivalent price move from average entry = 0.20 / effective_leverage (e.g. 4% at 5×, 2% at 10×). Express it as the bracket `take_profit` price and re-centre after each rung fill. ALSO check in software on every mark-price tick as a backup, because the exchange TP can be missed if the bracket is rejected.
- `tp_close_fraction` default 1.0 (close all). If < 1.0, close that fraction at target and trail the remainder with `trail_atr_mult` × ATR(1H) (default disabled).
- Time stop: close if the campaign is older than `max_campaign_days (14)` (positional hold) — config.

## 2.10 Sizing and leverage
- Leverage: `leverage` [ASK: use the leader's Recommended Margin as reference]; hard cap `max_leverage (10)`; set per-product order leverage via the leverage endpoint if needed (VERIFY path in docs).
- Campaign margin budget = min(`campaign_margin_pct (15%)` of equity, the amount implied by the 2.8 risk cap). Rung margin = budget × weight. Contracts via 1.6 sizing rule.
- Margin mode: `margin_mode: cross | isolated` [ASK], default `cross`. If isolated, implement the Auto Topup / Add-Remove position margin endpoints behind a flag, default off.

## 2.11 Portfolio-level limits
`max_concurrent_campaigns (3)`, `max_total_margin_pct (40%)`, `max_daily_loss_pct (3%)` → no new campaigns until next 00:00 UTC, `max_drawdown_pct (10%)` → flatten and halt. If mark price comes within `2 × ATR(4H)` of the position's liquidation price → cancel unfilled rungs and reduce the campaign by half.

## 2.12 State machine (per symbol)
`IDLE → ARMED (zone + bias valid) → TRIGGERED (sweep confirmed) → BUILDING (rung1 filled, rungs resting) → MANAGING (all/partial filled, bracket live) → EXITING → COOLDOWN → IDLE`. Persist state + the feature snapshot at each transition. Any exchange/DB mismatch → `HALTED`.

## 2.13 Calibration targets
I will paste the leader's published stats and trade history into `calibration_targets.yaml` (ROI, win rate, max drawdown, average holding time, typical leverage, instruments). The backtest `calibrate` command must report how far your results are from these and which parameters (`min_zone_score`, `sweep_min_atr`, `ladder_atr_mult`, `leverage`, `profit_target_pct_of_margin`) move them most.

## 2.14 Known risks you must surface in the README
Averaging into a losing position (rungs `against`) behaves like a mild martingale and can produce large drawdowns before a stop; tokenized metals can gap and show wider spreads; energy products may not be listed; funding costs accrue on multi-day holds; a ~20%-of-margin target at high leverage is a very small price move, so fees and slippage matter a lot — model them.

# PART 3 — ENGINEERING REQUIREMENTS (apply to the whole project)

## 3.1 Stack and layout
- Python 3.11+, `asyncio`, `httpx` (REST), `websockets` (streams), `pydantic` v2 (config + models), `PyYAML`, `pandas`/`numpy` (indicators + backtest), `sqlite3` (state), `pytest`. No heavy trading frameworks.
- Layout: `config.yaml`, `src/delta/{auth.py,rest.py,ws.py,ratelimit.py,products.py,orders.py,heartbeat.py}`, `src/core/{indicators.py,ledger.py,risk.py,state.py,scheduler.py}`, `src/strategy/…` (see Part 2), `src/backtest/{engine.py,fills.py,report.py}`, `src/main.py`, `tests/`, `README.md`.

## 3.2 Modes
`--mode backtest | paper | live`. `paper` = real market data from prod/testnet WebSocket, simulated fills, zero orders sent. `live` on testnet is the default live target; prod needs the explicit flag from 1.1. The same strategy code must run unchanged in all three modes (strategy talks to an `ExecutionPort` interface with `BacktestPort`, `PaperPort`, `DeltaPort` implementations).

## 3.3 State, idempotency, recovery
- SQLite is the source of truth for the strategy's own bookkeeping (campaigns / lots / hedges / orders / events). Every state transition is written BEFORE the corresponding order is sent and confirmed after.
- On startup and after every reconnect: (1) fetch open orders, positions, balances via REST; (2) rebuild internal state from the DB; (3) reconcile: the sum of open lots per symbol must equal the exchange net position (in contracts, correct sign). Any mismatch → halt that symbol/sleeve, send an alert, require manual `--resume`.
- Crash-safe: killing the process at any line and restarting must never double-send an order or lose a lot.

## 3.4 Risk controls that are NOT optional
- Global kill switch: a file `KILL` in the working dir (and SIGTERM) → cancel all open orders, optionally flatten (config), stop.
- Max daily loss, max drawdown from peak equity (halt + optionally flatten), max margin utilisation, max gross notional / equity, liquidation-distance monitor (use the `liquidation_price` / margin fields from the positions endpoint and mark price).
- Never place an order if: product not `operational`, spread filter fails, price outside `price_band`, computed size is 0 or above `position_size_limit`, or the risk checker vetoes.
- Dry-run print of every intended order (symbol, side, size, price, reason, risk numbers) before it is sent, logged as structured JSON.

## 3.5 Observability
Structured JSON logs; one `events` table row per decision with the full feature snapshot that caused it (why did it enter, why did it skip). Optional Telegram alerts (fills, TP, SL, halts, mismatches, reconnects) via env vars. A `report` CLI that prints open campaigns/lots, PnL, margin usage, and sleeve/allocation state.

## 3.6 Backtester requirements
- Event-driven on candles (1m base so limit-touch and SL/TP ordering are realistic), conservative fill rules: a resting limit fills only if price trades THROUGH it by ≥ 1 tick (or touches it with 50% probability setting off by default); when SL and TP are both inside one candle assume SL first; market orders fill at next-bar open ± slippage (config bps).
- Costs: use each product's `maker_commission_rate` / `taker_commission_rate` from /products, plus funding payments from historical funding if available, plus option premium/fees where options are used.
- Report: total return, CAGR, max drawdown, Sharpe/Sortino (daily), profit factor, win rate, average/median campaign duration, average margin used, worst adverse excursion per campaign, number of scale-in rungs filled distribution, share of exits by reason, monthly returns table, equity curve CSV + PNG.
- CLI parameter sweep + walk-forward split (train/validate) and a "calibration" command that compares the backtest's stats against the leader's published stats (I will paste them into `calibration_targets.yaml`).
- No lookahead: indicators computed on CLOSED candles only; higher-timeframe values are lagged by one completed HTF bar.

## 3.7 Tests (pytest)
Signature signer (independent HMAC recomputation, not the docs sample), rate-limiter, size/tick rounding, pivot/sweep detector on hand-built candle fixtures, ladder math, LIFO ledger invariants, reconciliation mismatch handling, restart idempotency (simulate crash between "write state" and "send order"), kill switch, and one end-to-end backtest on synthetic data.

## 3.8 Deliverables and acceptance
Deliver the full repo, a README (setup, key creation with Trading permission + IP whitelist, config reference, how to run backtest → paper → testnet live), and `ASSUMPTIONS.md` listing every place where this spec was ambiguous and what you chose. Acceptance: all tests pass; backtest runs end-to-end on 12+ months of downloaded candles; paper mode runs 24h without exceptions; every parameter marked [ASK] in Part 2 is read from config and the program refuses to start in live mode while any [ASK] value is still null.

## 3.9 Honesty rules for you (the coding assistant)
- Do not invent API fields or endpoints. If the docs do not show it, say so and leave a clearly marked TODO with how to verify.
- This strategy spec is INFERRED from a public marketing description, not from the leader's real rules. Say so in the README. Do not claim the bot reproduces the leader's results.
- Not financial advice; leveraged derivatives can lose more than the deposit. Default everything to testnet and small size.
