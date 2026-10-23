# fund-manager — Delta Exchange India multi-strategy bots

Two production-grade, restart-safe bots in one repo, built from the specs in
`prompts/` (the specs are written from a **public marketing description of a
leader's strategies — inferred, not the leader's real rules**; these bots do
not claim to reproduce the leader's results):

| Strategy | Package | Spec |
| --- | --- | --- |
| `rwa_positional` | `src/strategy/rwa_positional/` | RWA metals & energy, positional: market-structure bias, key levels, liquidity sweeps, scale-in ladder, ≈20%-of-margin targets |
| `all_in_one` | `src/strategy/all_in_one/` | Multi-strategy portfolio: ETH covered call + LIFO, BTC covered put + LIFO, gold CC/CP modes, US-stock accumulate→distribute, dynamic allocation |

Both share `src/delta/` (Delta Exchange **India** REST + WS client, auth
signing, rate limiter, deadman switch) and `src/core/` (pydantic models,
ExecutionPort/StrategyPort contracts, LIFO ledger, indicators, global risk
monitor), plus one event-driven backtester in `src/backtest/`.

Execution ports — the identical strategy code runs in every mode (spec 3.2):
`backtest.engine.BacktestPort` (simulated fills),
`paper.port.PaperPort` (real market data, simulated fills, zero orders),
`live.port.DeltaPort` (real orders, WS streams, deadman switch).

> ⚠️ Not financial advice; leveraged derivatives can lose more than the
> deposit. Defaults are testnet/paper and small size. See `ASSUMPTIONS.md`.

## Paper trading on GitHub Actions (real data, simulated fills, no keys)

A scheduled workflow runs a **bounded paper session** every 4 hours
(`.github/workflows/paper.yml`) with a job per strategy — **A**
(`rwa_positional`) and **B** (`all_in_one`), as *matrix* jobs. Both use the
same strategy + port code as `--mode paper`, driven from **real Delta market
data** with **simulated fills** and **zero orders sent**. No API keys or
GitHub secrets are required. Each job journals to its own dir and commits it
back, so the simulated equity, open positions and strategy campaigns survive
restarts — and **strategies A and B keep fully separate simulated accounts
and PnL**:

| Dir | What's in it |
| --- | --- |
| `paper/REPORT.md` | Combined report: summary table, then **PnL calendar + daily stats per strategy** |
| `paper/<strategy>/daily.csv` | day-anchored snapshots: MTM equity, day PnL, fees, positions |
| `paper/<strategy>/trades.csv` | append-only fill journal |
| `paper/<strategy>/paper_state.json`, `paper/<strategy>/*.db*` | simulated account + strategy state |

Manual run: **Actions → paper-trading → Run workflow** (strategies / minutes /
initial equity inputs). Locally: `PYTHONPATH=src python -m tools.paper_session
--strategy rwa_positional` then `python -m tools.paper_report --out-dir paper
--readme README.md`.

<!-- paper:start -->

| Strategy | Equity | P&L | P&L % | Days (G/R) | Max DD | Fees |
|---|---|---|---|---|---|---|
| `all_in_one` (B) | $100,000.00 | +$0.00 | +0.00% | 2 (0/0) | +0.00% | $0.00 |
| `rwa_positional` (A) | $104,737.56 | +$4,737.56 | +4.74% | 2 (1/1) | +0.00% | $26.51 |

Full per-strategy **PnL calendar + daily stats** in [paper/REPORT.md](paper/REPORT.md).

> Simulated fills on real market data, separate account per strategy; no real-money orders are ever sent.

<!-- paper:end -->

## Safety gates

- `env: prod` requires the CLI flag `--i-understand-this-is-live`.
- Live mode refuses to start while any `[ASK]` value in `config.yaml` is still
  `null` (`src/core/config.py::ASK_KEYS` lists exactly what must be filled).
- A `KILL` file in the working directory (or SIGTERM) cancels all open orders
  (flattening optional via `execution.kill_flatten`).
- Cross margin only: run this on a **dedicated account/subaccount** holding
  only this strategy's capital; both prompts mandate the global limits in
  2.10 / 2.11 for this reason.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]

# keys: create on demo.delta.exchange (testnet) or prod console with
# Trading permission and IP whitelisting (IPv4 and IPv6 are separate entries).
export DELTA_API_KEY=...
export DELTA_API_SECRET=...
```

## Run order (recommended)

```bash
# 1. backtest on cached/downloadable candles
python -m main --mode backtest --strategy rwa_positional

# 2. paper: real market data, simulated fills, zero orders sent
python -m main --mode paper --strategy rwa_positional

# 3. live against TESTNET keys (default env)
python -m main --mode live --strategy rwa_positional

# 4. prod only then: --env prod ... --i-understand-this-is-live
```

Other commands:

```bash
python -m main report       # open campaigns/lots, PnL, margin, sleeve state
python -m main calibrate    # backtest vs calibration_targets.yaml
pytest -q                   # full test suite
```

## Configuration

Everything lives in `config.yaml` (per-strategy subtrees `rwa_positional:` and
`all_in_one:`). Every number in the prompts is a config key; points that the
spec marked `[ASK]` are `null` and must be filled before live trading:
`recommended_margin`, `min_turnover_usd`, `leverage`, `margin_mode`
(strategy A); per-sleeve lot notionals, `strategy_capital_usd`, base weights,
`stock_symbols` (strategy B).

## Honesty notes

- API details marked `TODO(VERIFY)` in code are not confirmed word-for-word in
  the docs; check the live docs before relying on them. Do not use the docs'
  sample signature as a test vector (it does not reproduce).
- Option P&L in backtests is an approximate Black-Scholes model (realised vol
  with an IV premium), clearly labelled in every report.
- Tokenized RWA / stock perp availability on Delta India changes; the bot
  discovers and verifies symbols at startup and skips what is missing loudly.
