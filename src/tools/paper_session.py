"""Bounded paper-trading session, designed for GitHub Actions (and cron).

Runs the *same* strategy + port code as `python -m main --mode paper` but for a
bounded wall-clock window (Actions jobs must terminate), then journals
everything the committed report needs under `<out-dir>/<strategy>/`:

  paper/<strategy>/<strategy>.db      strategy state (campaigns, lots, events) — survives restarts
  paper/<strategy>/paper_state.json   simulated account: equity + open positions (PaperPort)
  paper/<strategy>/trades.csv         append-only fill journal
  paper/<strategy>/daily.csv          day-anchored equity snapshots (upserted per UTC day)
  paper/<strategy>/meta.json          initial equity / strategy / created_at

Zero secrets: paper uses only public market data and sends no orders.
No API keys required; the [ASK] config gates apply to live mode only.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import os
import signal
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from core.config import Config, load_config
from core.models import FillEvent, now_utc
from paper.port import PaperPort

log = logging.getLogger("fm.paper_session")

TRADE_FIELDS = [
    "ts", "symbol", "side", "size", "price", "fee",
    "contract_value", "notional_usd", "reduce_only", "client_order_id",
]
DAILY_FIELDS = ["date", "ts", "equity", "cash", "unrealized", "open_positions", "sessions"]

RESOLUTION_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1H": 3600, "2H": 7200, "4H": 14400, "1D": 86400,
}


def build_strategy(cfg: Config, state_dir: Path):
    """Same wiring as main.build_strategy, but state lives in the CI journal dir."""
    name = cfg.flags.get("strategy") or "rwa_positional"
    if "rwa_positional" in name:
        from strategy.rwa_positional import RwaPositionalStrategy

        return RwaPositionalStrategy(config=cfg, db_path=str(state_dir / f"{name}.db"))
    from strategy.all_in_one import AllInOneStrategy

    return AllInOneStrategy(cfg.all_in_one or {}, state_dir=str(state_dir), log_dir=cfg.log_dir)


def strategy_symbols(strategy: Any, port: PaperPort, cfg: Config) -> list[str]:
    """Best-effort driver universe: strategy's resolved symbols > flags > catalog."""
    symbols = getattr(strategy, "symbols", None) or []
    if not symbols:
        resolved = getattr(strategy, "resolved_symbols", None)
        if isinstance(resolved, dict):
            symbols = sorted({s for lst in resolved.values() for s in (lst or [])})
    if not symbols:
        symbols = list(cfg.flags.get("symbols") or [])
    if not symbols:
        symbols = [p.symbol for p in sorted(port.catalog.all(), key=lambda p: p.symbol)][:12]
    return symbols


class Journal:
    """CSV journaling with per-UTC-day upsert for the daily snapshot row."""

    def __init__(self, trades_path: Path, daily_path: Path):
        self.trades_path = trades_path
        self.daily_path = daily_path
        trades_path.parent.mkdir(parents=True, exist_ok=True)
        if not trades_path.exists():
            trades_path.write_text(",".join(TRADE_FIELDS) + "\n")
        if not daily_path.exists():
            daily_path.write_text(",".join(DAILY_FIELDS) + "\n")

    def append_trades(self, fills: list[tuple[FillEvent, Decimal, Decimal]]) -> None:
        """Append (fill, contract_value, notional_usd) rows."""
        if not fills:
            return
        with self.trades_path.open("a", newline="") as fh:
            w = csv.writer(fh)
            for fill, cv, notional in fills:
                w.writerow([
                    fill.ts.isoformat(), fill.symbol, fill.side.value, fill.size,
                    format(fill.price, "f"), format(fill.fee, "f"),
                    format(cv, "f"), format(notional, "f"),
                    int(fill.reduce_only), fill.client_order_id,
                ])

    def _read_daily(self) -> tuple[list[dict[str, str]], csv.DictReader | None]:
        with self.daily_path.open(newline="") as fh:
            return list(csv.DictReader(fh)), None

    def upsert_daily(self, ts: datetime, equity: Decimal, cash: Decimal,
                     unrealized: Decimal, positions: dict[str, int]) -> None:
        """Replace today's row (sessions may run several times a day)."""
        date = ts.strftime("%Y-%m-%d")
        rows, _ = self._read_daily()
        sessions = 1
        for r in rows:
            if r["date"] == date:
                sessions = int(r.get("sessions") or 0) + 1
                break
        rows = [r for r in rows if r["date"] != date]
        rows.append({
            "date": date,
            "ts": ts.isoformat(),
            "equity": str(equity),
            "cash": str(cash),
            "unrealized": str(unrealized),
            "open_positions": json.dumps(positions, sort_keys=True),
            "sessions": str(sessions),
        })
        rows.sort(key=lambda r: r["date"])
        tmp = self.daily_path.with_suffix(self.daily_path.suffix + ".tmp")
        with tmp.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=DAILY_FIELDS)
            w.writeheader()
            w.writerows(rows)
        tmp.replace(self.daily_path)


async def run_session(cfg: Config, minutes: float, out_dir: Path) -> dict[str, Any]:
    cfg.mode = "paper"
    out_dir.mkdir(parents=True, exist_ok=True)
    if cfg.flags.get("dry_run_log") is None:
        cfg.flags["dry_run_log"] = True

    port = PaperPort(cfg)  # picks up paper_state_path + restores prior session
    strategy = build_strategy(cfg, out_dir)
    await port.bootstrap()
    await strategy.bootstrap(port)

    # meta: first ever session records the baseline equity
    meta_path = out_dir / "meta.json"
    if not meta_path.exists():
        meta_path.write_text(json.dumps({
            "strategy": cfg.flags.get("strategy") or "rwa_positional",
            "initial_equity": str(port.equity_sim),
            "created_at": now_utc().isoformat(),
        }, indent=2))
    journal = Journal(out_dir / "trades.csv", out_dir / "daily.csv")

    resolutions = cfg.flags.get("resolutions") or ["1m", "15m", "1H", "4H", "1D"]
    symbols = strategy_symbols(strategy, port, cfg)
    log.info("paper session: %s for %.0f min, symbols=%s",
             cfg.flags.get("strategy"), minutes, symbols)

    stop = asyncio.Event()

    def _sig(*_a):
        log.info("signal received — wrapping up and saving state")
        stop.set()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(s, _sig)
        except NotImplementedError:
            signal.signal(s, _sig)

    fills_seen = 0
    last_timer = time.monotonic()
    deadline = time.monotonic() + minutes * 60.0
    poll_interval = int(cfg.flags.get("poll_interval_s", 20))
    last_candle_ts: dict[tuple[str, str], int] = {}

    def journal_new_fills() -> int:
        fills = port.pending_fills
        port.pending_fills = []
        rows = []
        for f in fills:
            cv = port._contract_value(f.symbol)
            mark = port.positions.get(f.symbol, {}).get("mark")
            notional = Decimal(f.size) * f.price * cv
            rows.append((f, cv, notional))
            log.info("paper fill %s %s %s @ %s fee=%s", f.side.value, f.size,
                     f.symbol, f.price, f.fee)
        journal.append_trades(rows)
        return len(rows)

    while not stop.is_set() and time.monotonic() < deadline:
        try:
            end_s = int(time.time())
            for sym in symbols:
                for res in resolutions:
                    step = RESOLUTION_SECONDS.get(res, 60)
                    start = end_s - step * 3
                    candles = await port.candles(sym, res, start, end_s)
                    for c in candles or []:
                        if int(c.ts.timestamp()) <= last_candle_ts.get((sym, res), 0):
                            continue
                        last_candle_ts[(sym, res)] = int(c.ts.timestamp())
                        await strategy.on_candle_close(c)
                t = await port.ticker(sym)
                if t and t.mark_price:  # keep MTM marks fresh for snapshot/report
                    p = port.positions.get(sym)
                    if p:
                        p["mark"] = t.mark_price
                if t:
                    await strategy.on_ticker(sym, t)
            fills_seen += journal_new_fills()
            if time.monotonic() - last_timer >= 60:
                last_timer = time.monotonic()
                await strategy.on_timer()
        except Exception:
            log.exception("session loop error (continuing)")
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass

    # ---- close of session: finalize fills, snapshot, persist ----------------
    fills_seen += journal_new_fills()
    now = now_utc()
    cash = port.equity_sim
    unreal = port.unrealized_pnl()
    equity = cash + unreal
    open_pos = {sym: int(p["size"]) for sym, p in port.positions.items() if int(p["size"]) != 0}
    journal.upsert_daily(now, equity, cash, unreal, open_pos)
    port.save_state()
    log.info("session done: fills=%d equity=%s cash=%s unrealized=%s open=%s",
             fills_seen, equity, cash, unreal, open_pos)
    await port.aclose()
    return {"fills": fills_seen, "equity": str(equity), "out_dir": str(out_dir)}


def main() -> None:
    ap = argparse.ArgumentParser(description="bounded paper session for CI")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--strategy", default=None, help="rwa_positional | all_in_one")
    ap.add_argument("--minutes", type=float,
                    default=float(os.environ.get("PAPER_SESSION_MINUTES", 20)))
    ap.add_argument("--out-dir", default="paper")
    ap.add_argument("--initial-equity", type=float, default=None,
                    help="override paper_initial_equity_usd")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load_config(args.config)
    # every strategy keeps its own simulated account + journal under
    # <out-dir>/<strategy>/ so A (rwa_positional) and B (all_in_one) never mix
    strategy_name = args.strategy or "rwa_positional"
    cfg.flags["strategy"] = strategy_name
    if args.initial_equity is not None:
        cfg.flags["paper_initial_equity_usd"] = args.initial_equity
    out_dir = Path(args.out_dir) / strategy_name
    cfg.flags["paper_state_path"] = str(out_dir / "paper_state.json")

    result = asyncio.run(run_session(cfg, args.minutes, out_dir))
    print(json.dumps(result))
    # report generation is a separate command: python -m tools.paper_report


if __name__ == "__main__":
    main()
