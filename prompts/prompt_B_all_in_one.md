# PROMPT B — "All-in-One Delta Trades — Multi-Strategy Portfolio" bot for Delta Exchange India

## ROLE
You are a senior quantitative developer. Build a production-grade, restart-safe multi-sleeve trading bot in Python that recreates the portfolio strategy described below on Delta Exchange India, plus a backtester. Follow Part 1 (API reference), Part 2 (strategy), Part 3 (engineering). Read everything first, then list assumptions in `ASSUMPTIONS.md` and build. Any parameter tagged [ASK] must be a config key defaulting to `null` and blocks live trading until I fill it.

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

# PART 2 — STRATEGY SPEC: `all_in_one`

## 2.1 What is KNOWN (the leader's public description, paraphrased)
- Header note: USE CROSS MARGIN MODE ONLY.
- A multi-strategy portfolio combining the leader's proven setups into one diversified strategy, deployed across crypto, RWA assets and US equities, with allocation and the mix of setups adapting to prevailing market conditions (not a fixed combination).
- Components:
  1. ETH: Covered Call + LIFO position management.
  2. BTC: Covered Put + LIFO position management.
  3. XAUT and PAXG (tokenized gold): Covered Call and Covered Put strategies combined with LIFO management.
  4. US stocks: systematic accumulation during market declines, followed by distribution as prices rise and momentum weakens.
- Objective: combine several income-generating and accumulation approaches into one portfolio that adapts.
- Not stated anywhere (so [ASK] or assume and document): exact entry triggers, strikes/expiries, lot sizes, leverage, allocation weights, stops, and what "LIFO position management" means operationally. My interpretation of LIFO: positions are built as discrete LOTS and the MOST RECENTLY opened lot is always the first one closed (last-in, first-out).

## 2.2 Sleeves and global architecture
Four sleeves sharing one cross-margined account: `ETH_CC`, `BTC_CP`, `GOLD` (XAUTUSD + PAXGUSD), `STOCKS` (US equity perps). A `PortfolioManager` owns capital allocation and risk; each sleeve is a class implementing `on_candle_close()`, `on_tick()`, `on_fill()`, `desired_orders()`, `status()`. Sleeve status ∈ `ACTIVE | ACCUMULATE_ONLY | DISTRIBUTE_ONLY | PAUSED`. All sleeves use the same `ExecutionPort` and the same lot ledger.

## 2.3 Cross margin enforcement (hard requirement)
At startup read the account's margin mode (docs → Account → trading preferences / Change margin mode). If it is not cross: abort with a clear message, unless `--auto-set-cross` is passed AND there are no open positions/orders, in which case call the documented change-margin-mode endpoint. Re-check every hour; if it ever differs, halt new orders. Because cross margin means one bad sleeve can consume the whole account's margin, the account used must be a DEDICATED account or subaccount holding only this strategy's capital (docs list subaccount endpoints), and the global limits in 2.10 are mandatory.

## 2.4 LIFO lot ledger (shared by all sleeves)
SQLite tables: `lots(id, sleeve, symbol, side, contracts, entry_price, opened_at, status[open|closed], close_price, closed_at, entry_client_oid, exit_client_oid, tp_price)`, `hedges(id, sleeve, option_symbol, product_id, side, contracts, avg_price, opened_at, status)`, `orders`, `events`.
Rules:
- Every entry fill creates ONE lot. Strict LIFO (`lifo_strict: true`): only the NEWEST open lot of a (sleeve, symbol) may be closed; when it closes, the next-newest becomes eligible. Older lots are never closed while a newer one is open (except by global de-risk, which also goes newest-first, or the kill switch which flattens everything).
- Exchange nets positions per product, so the ledger is authoritative for lot identity. Invariant: sum of open lot contracts (signed) per symbol == exchange position size. Violation → halt that sleeve.
- Exit mechanics: keep exactly one resting reduce-only limit TP for the newest lot only (`lot.tp_price`); on its fill update the ledger and place the TP for the new newest lot. Software fallback checks mark price each tick.

## 2.5 Option-leg manager (covered call / covered put), used by ETH_CC, BTC_CP and GOLD where options exist
Definitions I want implemented: COVERED CALL = long underlying perp lots + SHORT call options covering them. COVERED PUT = short underlying perp lots + SHORT put options covering them. (If you believe Mirrorpip's "covered" structure means something else, implement this standard definition and put the alternative in ASSUMPTIONS.md behind `hedge_style`.)
- Instruments: perp `ETHUSD` / `BTCUSD` for the underlying leg; option symbols like `C-ETH-<strike>-<ddmmyy>` / `P-BTC-<strike>-<ddmmyy>` discovered from GET /tickers?contract_types=call_options (or put_options)&underlying_asset_symbols=ETH|BTC&expiry_date=DD-MM-YYYY. Never hard-code option symbols.
- Selection: expiry with DTE in [`opt_min_dte (5)`, `opt_max_dte (10)`] (config; prefer weekly); strike whose |delta| (ticker `greeks.delta`) is closest to `opt_target_delta (0.20)` within [0.12, 0.30]; require `spread% ≤ opt_max_spread_pct (8%)`, `oi ≥ opt_min_oi`, and a positive bid.
- Coverage: target short contracts = floor( underlying_coin_exposure × `coverage_ratio (1.0)` / option.contract_value ), where underlying_coin_exposure = |sum of open lot contracts| × perp.contract_value. NEVER let short options exceed the underlying exposure (assert this on every order; it is what makes it "covered"). Manage the option position at SLEEVE level (not per lot), re-targeting whenever lots open/close.
- Order handling for options: limit orders only, start at mid, reprice toward the touch every `opt_reprice_s (10)` up to `opt_max_reprices (6)`, then give up and retry next cycle. Never market orders on options.
- Roll/close rules: buy back when ≥ `opt_buyback_capture_pct (80)`% of premium has decayed; at DTE ≤ 1 let it expire (assume cash settlement — VERIFY in the product docs) and sell the next cycle's option; if |delta| > `opt_roll_delta (0.45)` (threat of finishing in-the-money) roll out/up (call) or out/down (put) only for a net credit, otherwise leave it.
- If no options exist for a symbol (likely for XAUT/PAXG — detect it), run that sleeve in `hedge_mode: none` = the perp lot ladder with LIFO only, and log a startup warning. Do not fake an option leg with perps.
- Option margin is paid from the same cross-margin pool: include it in all margin-utilisation checks.

## 2.6 Sleeve ETH_CC (covered call + LIFO, long bias)
- Underlying: long `ETHUSD` perp lots.
- First lot when the sleeve turns ACTIVE and the regime filter passes (2.9). Further lots on dips: open a new lot when mark ≤ (price of the newest open lot) − `lot_spacing_atr (1.0)` × ATR(14, 1D), up to `max_lots (5)`.
- Lot size = sleeve_budget / `max_lots`, converted to contracts via 1.6, at sleeve leverage ≤ `sleeve_leverage_cap (3)` [ASK actual].
- Exit (strict LIFO): newest lot's TP = lot_entry × (1 + `lot_tp_pct (2.0)`%) as a reduce-only limit.
- Hedge: 2.5 with calls.
- Stop: sleeve-level protective rule — if equity attributable to the sleeve falls `sleeve_stop_pct (15)`% below its high-water mark, switch to `DISTRIBUTE_ONLY` (no new lots; close newest lots first at market over `derisk_steps (3)` steps).

## 2.7 Sleeve BTC_CP (covered put + LIFO, short bias)
Mirror of ETH_CC: short `BTCUSD` perp lots; new lot when mark ≥ newest lot price + `lot_spacing_atr × ATR(14,1D)`; strict-LIFO cover (buy to close) at lot_entry × (1 − `lot_tp_pct`); hedge with SHORT PUTS sized by 2.5 (coverage = short coin exposure). Because a short perp + short put loses on rallies, the sleeve is only ACTIVE in the regimes allowed by 2.9, and `max_lots` / leverage are capped lower than ETH_CC by default (`max_lots 4`, leverage cap 2).

## 2.8 Sleeve GOLD (XAUTUSD, PAXGUSD) and sleeve STOCKS
GOLD:
- For each of XAUTUSD/PAXGUSD choose a MODE by regime (2.9): `CC_MODE` (long lots + short calls) in neutral/up regimes, `CP_MODE` (short lots + short puts) in neutral/down regimes. Since Delta nets positions per product, a symbol may only switch mode when its lot stack is empty and the regime flip has persisted `mode_switch_days (3)` days. Same lot-spacing, LIFO TP and hedge logic as 2.6/2.7 with gold-appropriate defaults (`lot_spacing_atr 0.8`, `lot_tp_pct 1.0`, leverage cap 3). XAUT and PAXG share ONE risk bucket (combined lots ≤ `max_lots`).
- If options do not exist for the symbol → `hedge_mode: none` (2.5).
STOCKS (systematic accumulation → distribution, long-only):
- Universe: discover live stock perps from /products (category/tags/description of listed US equities; symbols like NVDAXUSD, TSLAXUSD, AAPLXUSD, METAXUSD, AMZNXUSD, GOOGLXUSD — verify which are live); configurable allow-list `stock_symbols` [ASK] and `max_stock_symbols (6)`. Expect a lower max leverage on these (~25×); use ≤ `stock_leverage_cap (2)`.
- ACCUMULATE: open lot 1 when close ≤ rolling-`dd_lookback_days (30)` high × (1 − `dd_entry_pct (8)`%) AND RSI(14,1D) ≤ `rsi_accumulate (40)`. Add lots every further `lot_spacing_pct (5)`% below the newest lot, up to `stock_max_lots (5)`. Lot size = stock_sleeve_budget / (`stock_max_lots` × number_of_symbols).
- DISTRIBUTE (after the rebound, when momentum fades): trigger when RSI(14,1D) has been ≥ `rsi_distribute_floor (60)` within the last 5 days AND (RSI crosses below its 3-day SMA OR the daily MACD histogram falls two bars in a row OR close < EMA(10) after being above it). Then sell the NEWEST lot first (LIFO), at most `max_lots_sold_per_day (1)`, only if that lot is ≥ `min_lot_profit_pct (3)`% in profit; otherwise hold. Reduce-only limit orders near the touch.
- Liquidity/hours: tokenized equities can have wider spreads or reduced liquidity when the underlying US market is closed → apply the spread filter, use limit orders only, and widen `max_spread_pct` check by time-of-day (config table). Skip entries within `earnings_blackout_days` of earnings if I provide an `earnings.csv` (optional).
- Per-symbol stop: if a symbol is down `stock_symbol_stop_pct (25)`% from its average cost → stop adding, switch that symbol to DISTRIBUTE_ONLY.

## 2.9 Regime model (drives which strategies are active)
Computed daily at 00:00 UTC and on 4H closes per underlying (ETH, BTC, XAUT, each stock):
- Trend score = sign and normalised distance of close vs EMA(50,1D) and EMA(200,1D) plus EMA(50) slope over 10 days → {strong_down, down, neutral, up, strong_up}.
- Volatility = ATR(14,1D)/close and its 90-day percentile.
- Drawdown = distance from the 90-day high.
Activation rules (all configurable): ETH_CC ACTIVE in {neutral, up}, ACCUMULATE_ONLY in {down}, PAUSED in strong_down; BTC_CP ACTIVE in {neutral, down}, DISTRIBUTE_ONLY/PAUSED in {up, strong_up}; GOLD mode per 2.8; STOCKS ACCUMULATE when drawdown ≥ `dd_entry_pct`, DISTRIBUTE_ONLY after a rebound with fading momentum, otherwise ACTIVE-hold. High volatility (percentile > 85) halves new-lot size.

## 2.10 Dynamic capital allocation
- Base weights (config, [ASK]) e.g. ETH_CC 30%, BTC_CP 20%, GOLD 25%, STOCKS 25% of `strategy_capital` (capital = account equity or a fixed amount; never exceed it), plus a `cash_reserve_pct (20)` that is never allocated.
- Daily target weight = base weight × regime_fit × inverse-vol factor, clamped to [`w_min (5%)`, `w_max (40%)`] and re-normalised; smoothed: `w_new = 0.7·w_old + 0.3·w_target`.
- Reallocation only changes the budget for NEW lots; it never force-closes existing lots (except through the de-risk rules).
- Global limits (mandatory because of cross margin): `max_gross_notional_to_equity (3.0)`, `max_margin_utilisation_pct (50)`, `min_liq_distance_pct (25)` (distance from mark to the account's liquidation level from margin/position data) → breach: stop all new lots and options sales, de-risk newest-first until restored; `max_daily_loss_pct (3)`, `max_drawdown_pct (12)` → flatten + halt.

## 2.11 Scheduling
Daily jobs at 00:00 UTC (regime + allocation); 4H on-close jobs (sleeve decisions); 1-minute loop for TP/SL/option reprice and risk monitor; hourly margin-mode and reconciliation check; options cycle check every 15 min.

## 2.12 Backtest scope
Perp legs: simulate from 1m candles. Options: if historical option prices are not available from the API, implement a Black-Scholes approximation using historical realised vol with a configurable IV premium (`iv_premium 1.1`) and bid-ask haircut, clearly labelled "approximate" in the report; never claim option P&L is exact. Report per-sleeve and portfolio stats, correlation between sleeves, and margin utilisation over time.

## 2.13 Calibration targets
I will paste the leader's published stats and trade history into `calibration_targets.yaml`; add a `calibrate` command comparing your backtest to them, and report which of `opt_target_delta`, `lot_spacing_atr`, `lot_tp_pct`, `dd_entry_pct`, leverage caps and base weights have the most influence.

## 2.14 Known risks you must surface in the README
Short puts + short perp (BTC_CP) lose in sharp rallies; covered calls cap upside while the long perp keeps full downside; LIFO means older, deeper-underwater lots can be stuck while newer ones cycle; cross margin links all sleeves to one liquidation level; stock/gold perps can gap, have wider spreads, and funding accrues; options liquidity on Delta is thinner than on larger venues; tokenized RWA availability can change.

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
