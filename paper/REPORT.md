# Paper-trading report (A + B)

_Paper money for each strategy: simulated fills on real Delta market data; zero orders sent, no API keys used._
_Generated 2026-10-24 13:18 UTC._

| Strategy | Equity | P&L | P&L % | Days (G/R) | Max DD | Fees |
|---|---|---|---|---|---|---|
| `all_in_one` (B) | $100,000.00 | +$0.00 | +0.00% | 3 (0/0) | +0.00% | $0.00 |
| `rwa_positional` (A) | $105,573.04 | +$5,573.04 | +5.57% | 3 (1/2) | -0.52% | $31.85 |

> Strategies trade *separate simulated accounts*; their numbers are independent (per-strategy equity, PnL, fees).

---
## Strategy B — `all_in_one`

_Initial equity $100,000.00. Start 2026-10-22._

Equity **$100,000.00** · P&L **+$0.00** (+0.00%) · trading days **3** (green 0 / red 0) · max drawdown **+0.00%** · fees $0.00

- Closed lots: 0
- Open unrealized PnL: **+$0.00**
- Open positions: none
_(Option P&L, when applicable, is an approximate Black-Scholes model.)_

### PnL calendar
### October 2026

| Mon | Tue | Wed | Thu | Fri | Sat | Sun |
|---|---|---|---|---|---|---|
|   |   |   | · | · |   |   |
| · | · | · | · | · |   |   |
| · | · | · | · | · |   |   |
| · | · | · | **22** 0.00 | **23** 0.00 | **24** 0.00 |   |
| · | · | · | · | · |   |   |

_(UTC days. `**day** +$PnL` = recorded session day; `·` = weekday with
no session; weekends without data left blank. Future days left blank.)_

### Daily stats (last 30 recorded days)

| Date | Day PnL | Equity (MTM) | Cash | Unrealized | Trades | Fees | Sessions | Open positions |
|---|---|---|---|---|---|---|---|---|
| 2026-10-24 | +$0.00 | $100,000.00 | $100,000.00 | +$0.00 | 0 | $0.00 | 2 | — |
| 2026-10-23 | +$0.00 | $100,000.00 | $100,000.00 | +$0.00 | 0 | $0.00 | 4 | — |
| 2026-10-22 | +$0.00 | $100,000.00 | $100,000.00 | +$0.00 | 0 | $0.00 | 2 | — |

_(Day PnL: closing equity vs previous recorded day close; MTM includes
 unrealized PnL of open positions. Trades = fills journaled that day.)_


## Strategy A — `rwa_positional`

_Initial equity $100,000.00. Start 2026-10-22._

Equity **$105,573.04** · P&L **+$5,573.04** (+5.57%) · trading days **3** (green 1 / red 2) · max drawdown **-0.52%** · fees $31.85

- Closed lots: 0
- Open unrealized PnL: **+$5,604.89**
- Open positions: `PAXGUSD -9286`, `SLVONUSD -15282`, `USOONUSD -4956`, `XAUTUSD -15262`
_(Option P&L, when applicable, is an approximate Black-Scholes model.)_

### PnL calendar
### October 2026  — month +$5,573.04

| Mon | Tue | Wed | Thu | Fri | Sat | Sun |
|---|---|---|---|---|---|---|
|   |   |   | · | · |   |   |
| · | · | · | · | · |   |   |
| · | · | · | · | · |   |   |
| · | · | · | **22** -$843.73 | **23** +$6,963.64 | **24** -$546.87 |   |
| · | · | · | · | · |   |   |

_(UTC days. `**day** +$PnL` = recorded session day; `·` = weekday with
no session; weekends without data left blank. Future days left blank.)_

### Daily stats (last 30 recorded days)

| Date | Day PnL | Equity (MTM) | Cash | Unrealized | Trades | Fees | Sessions | Open positions |
|---|---|---|---|---|---|---|---|---|
| 2026-10-24 | -$546.87 | $105,573.04 | $99,968.15 | +$5,604.89 | 2 | $1.32 | 2 | PAXGUSD-9286, SLVONUSD-15282, USOONUSD-4956, XAUTUSD-15262 |
| 2026-10-23 | +$6,963.64 | $106,119.91 | $99,969.47 | +$6,150.44 | 9 | $8.42 | 4 | PAXGUSD-9286, SLVONUSD-15282, USOONUSD-4956, XAUTUSD-11964 |
| 2026-10-22 | -$843.73 | $99,156.27 | $99,977.89 | -$821.62 | 8 | $22.11 | 3 | PAXGUSD-9286, SLVONUSD-7566, USOONUSD-4956 |

_(Day PnL: closing equity vs previous recorded day close; MTM includes
 unrealized PnL of open positions. Trades = fills journaled that day.)_

