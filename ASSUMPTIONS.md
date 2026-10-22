# ASSUMPTIONS.md

Recorded while implementing the two bots from the prompt specs
(`prompts/prompt_A_rwa_positional.md`, `prompts/prompt_B_all_in_one.md`).
Both specs state they are INFERRED from a public marketing description of a
leader's strategies — NOT the leader's real rules. These bots therefore
instantiate our concrete interpretation; no claim is made of reproducing the
leader's published results. Not financial advice; leveraged derivatives can
lose more than the deposit.

## System-wide choices

1. **One repo, two strategies.** Prompt A and B share Part 1 (Delta India API)
   and Part 3 (engineering). We implement a single monorepo with
   `src/delta/` (client), `src/core/` (models/ports/ledger/indicators/scheduler),
   `src/strategy/rwa_positional/`, `src/strategy/all_in_one/`,
   `src/backtest/` — each strategy uses its own SQLite file under `state/`.
2. **[ASK] keys.** Every parameter tagged [ASK] defaults to `null` in
   `config.yaml` and live mode refuses to start while any of them is still
   null. See `src/core/config.py::ASK_KEYS`.
3. **Same contract for all modes.** Strategies only talk to
   `core.ports.ExecutionPort`; `BacktestPort`, paper and `DeltaPort`
   implementations share identical strategy code.
4. **Unconfirmed API details** are marked `TODO(VERIFY)` in code (order-by-
   client-oid path, exact WS auth message/India socket URL, deadman endpoints,
   candle API units). The live docs win over anything here (specs, Part 1).

## Prompt A — rwa_positional

5. **Market structure.** "HH+HL/LH+LL" implemented as the last two confirmed
   fractal pivots per side (3-bar fractals on 4H, 2-bar on 1H per spec 2.3
   defaults).
6. **"≈20% of used margin"** implemented as: used_margin = notional /
   effective_leverage (from-and-fallback-to position endpoint `margin`);
   target price move = 0.20 / effective_leverage from avg entry. A software
   mark-price check runs every tick as backup to the exchange bracket TP.
7. **Sweep fallback fill** rule: entry post-only limits sit at best bid/ask at
   signal time.
8. **Gold bucket**: XAUTUSD and PAXGUSD share `max_campaigns_per_bucket: 1`.
9. **Energy products**: scanned by description/tags; if none is live the
   startup log states metals-only and continues.

## Prompt B — all_in_one

10. **LIFO.** Opened as distinct lots; newest lot only is closable (strict
    mode); global de-risk also goes newest-first; the kill switch flattens all.
11. **Covered call / covered put** = short options against matched perp
    exposure (long perp + short call; short perp + short put). The alternative
    leader interpretation is behind `hedge_style` in config.
12. **Options exist only for BTC/ETH** (probed at runtime via tickers). RWA
    symbols run `hedge_mode: none` (perp lot ladder only) with a startup warning.
13. **GOLD mode switch** (CC_MODE ↔ CP_MODE) only when the symbol's lot stack
    is empty AND the regime flip persisted `mode_switch_days`.
14. **Allocation** changes budgets for new lots only; it never force-closes
    positions (de-risk paths do that newest-first).
15. **Backtest option P&L** is a Black-Scholes approximation (realised vol ×
    `iv_premium`, bid/ask haircut) explicitly labelled approximate.
16. **Cross-margin enforcement**: bot checks margin mode each hour via the
    trading-preferences endpoint; if it isn't cross it halts new orders
    (auto-set allowed only when no open positions/orders, `--auto-set-cross`).

## Backtester conservatism

17. Limits fill only if the candle trades through them by ≥ 1 tick; the 50%-on-
    touch shortcut exists but defaults OFF. SL before TP when both are inside
    one candle. Market fills at next close ± `slippage_bps`.
18. Funding approximated as constant bps per 8h when historical funding data
    is unavailable; maker/taker fees from each product's published rates.
