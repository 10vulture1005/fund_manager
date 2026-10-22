"""fund-manager CLI (both bots, specs 3.1-3.5).

Commands:
  python -m main --strategy rwa_positional --mode paper
  python -m main --strategy all_in_one   --mode live
  python -m main --mode backtest --since-months 14
  python -m main report           # open campaigns/lots, PnL, margin, sleeves
  python -m main calibrate        # backtest vs calibration_targets.yaml

Live-safety gates (spec 1.1 + 3.8):
  - env prod requires the CLI flag --i-understand-this-is-live
  - live mode refuses to start while any [ASK] config value is still null
  - a KILL file in the working dir (or SIGTERM) cancels all open orders
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

import yaml

from core.config import Config, load_config
from core.models import new_client_order_id

log = logging.getLogger("fm.main")


def setup_logging(log_dir: str) -> None:
    for d in (log_dir, "state", "data", "reports"):
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
        except FileExistsError:
            pass
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    fh = logging.FileHandler(Path(log_dir) / "fm.log")
    fh.setFormatter(handler.formatter)
    logging.basicConfig(level=logging.INFO, handlers=[handler, fh])


def build_strategy(cfg: Config, now_fn=None):
    """Import strategy AFTER infra is installed so missing deps fail clearly."""
    if cfg.mode not in ("paper", "live", "backtest"):
        raise ValueError(f"unknown mode {cfg.mode}")
    if "rwa_positional" in (cfg.flags.get("strategy") or "rwa_positional"):
        from strategy.rwa_positional import RwaPositionalStrategy

        return RwaPositionalStrategy(cfg, now_fn=now_fn)
    from strategy.all_in_one import AllInOneStrategy

    return AllInOneStrategy(cfg.all_in_one or {}, state_dir=cfg.state_dir, log_dir=cfg.log_dir)


async def run(cfg: Config) -> None:
    setup_logging(cfg.log_dir)
    kill_file = Path("KILL")
    stop = asyncio.Event()

    def _sig(*_a):  # SIGTERM → stop (spec 3.4)
        log.info("SIGTERM received — initiating shutdown")
        stop.set()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(s, _sig)
        except NotImplementedError:
            signal.signal(s, _sig)

    from backtest.engine import BacktestPort, run_backtest  # noqa: F401

    if cfg.mode == "backtest":
        await cmd_backtest(cfg)
        return

    # paper / live share strategy code; ports differ
    if cfg.flags.get("dry_run_log") is None:
        cfg.flags["dry_run_log"] = True

    if cfg.mode == "live":
        chosen = cfg.flags.get("strategy") or "rwa_positional"
        cfg.assert_live_ready(chosen)

    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if cfg.mode == "paper":
        port = build_paper_port(cfg)
    else:
        port = build_live_port(cfg)

    strategy = build_strategy(cfg)
    await port.bootstrap()
    await strategy.bootstrap(port)

    resolutions = cfg.flags.get("resolutions") or ["1m", "15m", "1H", "4H", "1D"]
    symbols = cfg.flags.get("symbols") or []
    last_candle_ts: dict[tuple[str, str], int] = {}
    last_timer = 0.0

    async def diag():
        # KILL file tripwire (spec 3.4) + optionally flatten on shutdown
        while not stop.is_set() and not kill_file.exists():
            await asyncio.sleep(1.0)
        if kill_file.exists():
            log.info("KILL file present — cancel all open orders & halt (spec 3.4)")
            orders = await port.open_orders()
            for o in orders:
                await port.cancel(o.client_order_id, o.symbol)
            if cfg.flags.get("kill_flatten"):
                try:
                    from live.port import DeltaPort  # close-all only on live port

                    if isinstance(port, DeltaPort):
                        await port.rest.close_all_positions()
                except Exception:
                    log.exception("flatten-on-kill failed")
            stop.set()

    async def drain_fills() -> None:
        # ports append FillEvents; strategy must hear about all of them
        while True:
            fills = getattr(port, "pending_fills", [])
            if fills:
                port.pending_fills = []
                for f in fills:
                    await strategy.on_fill(f)
            await asyncio.sleep(1.0)

    async def poll_market() -> None:
        # REST polling driver: works unchanged on paper and live ports.
        # (Preferred WS streams live inside DeltaPort.add_ws_subscriptions +
        # DeltaWSClient; REST polling is the same-code fallback per spec 1.4.)
        import time as _time

        nonlocal last_timer
        while not stop.is_set():
            try:
                now = _time.time()
                end_s = int(now)
                if not symbols:
                    prods = await port.products()
                    symbols_local = [p.symbol for p in prods][:12]
                else:
                    symbols_local = symbols
                for sym in symbols_local:
                    for res in resolutions:
                        step = {"1m": 60, "3m": 180, "5m": 300, "15m": 900,
                                "30m": 1800, "1H": 3600, "2H": 7200, "4H": 14400,
                                "1D": 86400}.get(res, 60)
                        start = end_s - step * 3
                        candles = await port.candles(sym, res, start, end_s)
                        for c in candles or []:
                            if not getattr(c, "closed", True):
                                continue
                            key = (sym, res)
                            if int(c.ts.timestamp()) <= last_candle_ts.get(key, 0):
                                continue
                            last_candle_ts[key] = int(c.ts.timestamp())
                            await strategy.on_candle_close(c)
                    t = await port.ticker(sym)
                    if t:
                        await strategy.on_ticker(sym, t)
                if now - last_timer >= 60:
                    last_timer = now
                    await strategy.on_timer()
            except NotImplementedError:
                raise
            except Exception as e:
                log.exception("market poll error: %s", e)
            await asyncio.sleep(cfg.flags.get("poll_interval_s", 20))

    tasks: list[asyncio.Task] = [asyncio.create_task(diag()), asyncio.create_task(drain_fills()), asyncio.create_task(poll_market())]
    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()


def build_paper_port(cfg: Config):
    """Real market data, zero orders, simulated fills (spec 3.2)."""
    from paper.port import PaperPort

    return PaperPort(cfg)


def build_live_port(cfg: Config):
    """Real REST/WS ExecutionPort against Delta India."""
    from live.port import DeltaPort

    return DeltaPort(cfg)


async def cmd_backtest(cfg: Config) -> None:
    from datetime import timedelta

    from backtest.data import download_candles, load_candles, data_path
    from backtest.engine import BacktestPort, run_backtest

    bt = cfg.backtest
    strategy_name = bt.get("strategy") or cfg.flags.get("strategy") or "rwa_positional"
    symbol = bt.get("symbol", "XAUTUSD")
    resolution = bt.get("resolution", "1m")
    months = int(bt.get("months", 14))
    end = None
    import datetime as dt

    end = dt.datetime.now(dt.timezone.utc)
    start = end - timedelta(days=months * 30)
    path = data_path(cfg.data_dir, symbol, resolution)
    if path.exists():
        log.info("using cached candles %s", path)
        candles = load_candles(path, symbol, resolution)
    else:
        # candle history always comes from prod: it's a public endpoint and the
        # testnet CDN serves synthetic candles (huge anomalous wicks, limited
        # depth) that make risk sizing skip every signal.
        candles = await download_candles(
            cfg.api.prod_rest, symbol, resolution, start, end, cfg.data_dir
        )
    if not candles:
        log.error("no candles for %s %s — cannot run backtest", symbol, resolution)
        return
    products = {}
    # minimal synthetic product registry for the simulator; real catalog is used live
    from decimal import Decimal

    def fake_product(sym: str) -> dict:
        return {
            symbol: dict(
                symbol=sym,
                product_id=1,
                contract_type="perpetual_futures",
                tick_size=Decimal("0.1"),
                contract_value=Decimal("0.01"),
                default_leverage=Decimal("10"),
                maker_commission_rate=Decimal("0.0005"),
                taker_commission_rate=Decimal("0.0007"),
            )
        }

    from core.models import ProductInfo

    products = {
        symbol: ProductInfo(
            symbol=symbol,
            product_id=1,
            contract_type="perpetual_futures",
            tick_size=Decimal("0.1"),
            contract_value=Decimal("0.01"),
            default_leverage=Decimal("10"),
            maker_commission_rate=Decimal("0.0005"),
            taker_commission_rate=Decimal("0.0007"),
        )
    }
    port = BacktestPort(
        products,
        Decimal(str(bt.get("initial_equity_usd", 10000))),
        {symbol: candles},
    )
    if strategy_name == "rwa_positional":
        from strategy.rwa_positional import RwaPositionalStrategy

        strategy = RwaPositionalStrategy(cfg, now_fn=lambda: candles[-1].ts)
    else:
        from strategy.all_in_one import AllInOneStrategy

        strategy = AllInOneStrategy(cfg.all_in_one or {}, state_dir=cfg.state_dir)
    summary = await run_backtest(strategy, port, candles, report_dir=Path("reports"))
    log.info(
        "backtest done: initial=%s final=%s fills=%s (reports/equity_curve.csv, reports/summary.json)",
        summary["initial_equity"],
        f"{summary['final_equity']:.2f}",
        summary["fills"],
    )


def cmd_report(cfg: Config) -> None:
    """Print open campaigns/lots, PnL, margin, sleeve state (spec 3.5)."""
    import sqlite3

    dbs = [
        Path(cfg.state_dir) / "rwa_positional.db",
        Path(cfg.state_dir) / "aio_state.sqlite",
        Path(cfg.state_dir) / "portfolio.sqlite",
    ]
    any_db = False
    for db in dbs:
        if not db.exists():
            continue
        any_db = True
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        print(f"== {db.name} ==")
        for table in ("lots", "campaigns", "orders", "events"):
            try:
                cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
                if not cols:
                    continue
                where = "WHERE status IN ('open','BUILDING','MANAGING','TRIGGERED')" if "status" in cols else ""
                rows = con.execute(f"SELECT * FROM {table} {where} ORDER BY 1 DESC LIMIT 20").fetchall()
            except sqlite3.OperationalError:
                continue
            print(f"-- {table} ({len(rows)} shown) --")
            for r in rows:
                print(dict(r))
    if not any_db:
        print("no state db yet (run a backtest or paper session first)")


def cmd_calibrate(cfg: Config) -> None:
    report_path = Path("reports/report.json")
    if not report_path.exists():
        print("run a backtest first")
        return
    import json

    report = json.loads(report_path.read_text())
    from backtest.report import calibrate

    comparison = calibrate(report, Path("calibration_targets.yaml"))
    print(json.dumps(comparison, indent=2, default=str))


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="fund manager (Delta Exchange India)")
    ap.add_argument("--mode", default=None, help="backtest | paper | live")
    ap.add_argument("--strategy", default=None, help="rwa_positional | all_in_one")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--i-understand-this-is-live", action="store_true", help="prod gate")
    ap.add_argument("--resume", action="store_true", help="resume after intentional mismatch halt")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "report", "calibrate"])
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.mode:
        cfg.mode = args.mode
    if args.strategy:
        cfg.flags["strategy"] = args.strategy
    if args.i_understand_this_is_live:
        cfg.flags["i_understand_this_is_live"] = True
    cfg.flags["resume"] = args.resume

    if cfg.env == "prod" and not cfg.flags.get("i_understand_this_is_live"):
        raise SystemExit("prod requires --i-understand-this-is-live (spec 1.1)")
    if cfg.mode == "live":
        strategy_name = cfg.flags.get("strategy", "rwa_positional")
        cfg.assert_live_ready(strategy_name if isinstance(strategy_name, str) else "rwa_positional")

    if args.command == "report":
        cmd_report(cfg)
    elif args.command == "calibrate":
        cmd_calibrate(cfg)
    else:
        asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
