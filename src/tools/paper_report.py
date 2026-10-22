"""Committed paper-trading report: PnL calendar + daily stats, one section
per strategy (A: rwa_positional, B: all_in_one).

Reads the journals produced by `python -m tools.paper_session`, which stores
each strategy in its own sub-directory:

  <out-dir>/<strategy>/meta.json     initial equity / strategy / created_at
  <out-dir>/<strategy>/daily.csv     day-anchored snapshots (equity, cash, MTM)
  <out-dir>/<strategy>/trades.csv    fill journal
  <out-dir>/<strategy>/*.sqlite      strategy state (closed lots, win rate)

Writes:

  <out-dir>/REPORT.md                combined report: summary table, then a
                                     PnL calendar + daily stats per strategy
  README.md                          summary table between the
                                     <!-- paper:start/end --> markers

All numbers are paper-money (simulated fills, real market data, no orders sent).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("fm.paper_report")

WEEKDAY_HEAD = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
README_START = "<!-- paper:start -->"
README_END = "<!-- paper:end -->"
STRATEGY_LABEL = {  # A/B labels from the prompt specs (prompts/*.md)
    "rwa_positional": "A",
    "all_in_one": "B",
}


def _money(x: float | None, sign: bool = False) -> str:
    if x is None:
        return "—"
    s = f"{abs(x):,.2f}"
    if x < 0:
        return f"-${s}"
    return (f"+${s}" if sign else f"${s}")


def _pct(x: float, sign: bool = True) -> str:
    s = f"{abs(x):.2f}%"
    if x < 0:
        return f"-{s}"
    return (f"+{s}" if sign else s)


@dataclass
class DayRow:
    date: str
    equity: float
    cash: float = 0.0
    unrealized: float = 0.0
    positions: dict[str, int] = field(default_factory=dict)
    sessions: int = 0
    trades: int = 0
    fees: float = 0.0
    pnl: float = 0.0


def load_days(out_dir: Path, initial_equity: float) -> dict[str, DayRow]:
    days: dict[str, DayRow] = {}
    daily_path = out_dir / "daily.csv"
    if daily_path.exists():
        with daily_path.open(newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    positions = json.loads(r.get("open_positions") or "{}")
                except ValueError:
                    positions = {}
                days[r["date"]] = DayRow(
                    date=r["date"],
                    equity=float(r["equity"] or 0),
                    cash=float(r["cash"] or 0),
                    unrealized=float(r["unrealized"] or 0),
                    positions=positions,
                    sessions=int(r.get("sessions") or 0),
                )
    trades_path = out_dir / "trades.csv"
    if trades_path.exists():
        with trades_path.open(newline="") as fh:
            for r in csv.DictReader(fh):
                d = (r.get("ts") or "")[:10]
                if d not in days:
                    continue
                days[d].trades += 1
                days[d].fees += float(r.get("fee") or 0)
    # daily PnL: today's closing equity vs the previous *recorded* day close
    prev: float | None = None
    for d in sorted(days):
        base = prev if prev is not None else initial_equity
        days[d].pnl = days[d].equity - base
        prev = days[d].equity
    return days


def load_closed_lots(out_dir: Path, strategy: str) -> list[dict[str, Any]]:
    """Closed lots from the strategy sqlite — realized-PnL approximation per lot.

    Lot PnL is estimated as sign x (close - entry) x contracts x contract_value,
    where contract_value is taken from the most recent journal trade for that
    symbol (the db does not store it). Labelled as an approximation in output.
    """
    candidates = [
        out_dir / f"{strategy}.db",
        out_dir / "aio_state.sqlite",   # all_in_one ledger lives here (state_dir=out-dir)
        out_dir / "portfolio.sqlite",
    ]
    db = next((p for p in candidates if p.exists()), None)
    if db is None:
        return []
    cv: dict[str, float] = {}
    trades_path = out_dir / "trades.csv"
    if trades_path.exists():
        with trades_path.open(newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    cv[r["symbol"]] = float(r.get("contract_value") or 1)
                except (KeyError, ValueError):
                    continue
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT symbol, side, contracts, entry_price, close_price, closed_at "
            "FROM lots WHERE status='closed' ORDER BY closed_at"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    lots: list[dict[str, Any]] = []
    for r in rows:
        lot = dict(r)
        sign = 1.0 if (lot.get("side") or "buy").lower() in ("buy", "long", "b") else -1.0
        entry = float(lot.get("entry_price") or 0)
        close = float(lot.get("close_price") or 0)
        contracts = float(lot.get("contracts") or 0)
        lot["pnl"] = sign * (close - entry) * contracts * cv.get(lot["symbol"], 1.0)
        lots.append(lot)
    return lots


def max_drawdown(days: dict[str, DayRow]) -> float:
    peak = -float("inf")
    dd = 0.0
    for d in sorted(days):
        eq = days[d].equity
        peak = max(peak, eq)
        if peak > 0:
            dd = min(dd, (eq - peak) / peak * 100.0)
    return dd


def calendar_markdown(days: dict[str, DayRow]) -> str:
    """Month tables: one row per ISO week, Mon..Sun cells = day + PnL."""
    if not days:
        return "_No paper PnL data yet — first sessions pending._\n"
    by_month: dict[str, dict[str, DayRow]] = defaultdict(dict)
    for d, row in days.items():
        by_month[d[:7]][d] = row
    parts: list[str] = []
    for month in sorted(by_month):
        month_days = by_month[month]
        month_pnl = sum(r.pnl for r in month_days.values())
        year, mon = int(month[:4]), int(month[5:7])
        first = date(year, mon, 1)
        last = date(year + (mon == 12), (mon % 12) + 1, 1) - timedelta(days=1)
        month_total = ""
        if abs(month_pnl) > 0.005:
            month_total = f"  — month {_money(month_pnl, sign=True)}"
        parts.append(f"### {first.strftime('%B %Y')}{month_total}\n")
        parts.append("| " + " | ".join(WEEKDAY_HEAD) + " |")
        parts.append("|" + "---|" * 7)
        cur = first - timedelta(days=first.weekday())  # grid starts Monday
        while cur <= last:
            cells = []
            for wd in range(7):
                day = cur + timedelta(days=wd)
                if day < first or day > last:
                    cells.append(" ")  # pad outside the month
                    continue
                row = month_days.get(day.isoformat())
                if row is None:
                    # no snapshot that day: weekend stays empty, weekday shows ·
                    cells.append(" " if day.weekday() >= 5 else "·")
                    continue
                cell = f"**{day.day}**"
                if abs(row.pnl) > 0.005:
                    cell += f" {_money(row.pnl, sign=True)}"
                elif row.sessions:
                    cell += " 0.00"
                cells.append(cell)
            parts.append("| " + " | ".join(cells) + " |")
            cur += timedelta(days=7)
        parts.append("")
    parts.append("_(UTC days. `**day** +$PnL` = recorded session day; `·` = weekday with\n"
                 "no session; weekends without data left blank. Future days left blank.)_\n")
    return "\n".join(parts)


def daily_stats_table(days: dict[str, DayRow], n_days: int = 30) -> str:
    if not days:
        return "_No daily snapshots yet._\n"
    rows = [days[d] for d in sorted(days)][-n_days:][::-1]
    out = [
        "| Date | Day PnL | Equity (MTM) | Cash | Unrealized | Trades | Fees | Sessions | Open positions |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        pos = ", ".join(f"{s}{'+' if v > 0 else ''}{v}" for s, v in sorted(r.positions.items())) or "—"
        out.append(
            f"| {r.date} | {_money(r.pnl, sign=True)} | {_money(r.equity)} "
            f"| {_money(r.cash)} | {_money(r.unrealized, sign=True)} | {r.trades} "
            f"| {_money(r.fees)} | {r.sessions} | {pos} |"
        )
    out.append("")
    out.append("_(Day PnL: closing equity vs previous recorded day close; MTM includes\n"
               " unrealized PnL of open positions. Trades = fills journaled that day.)_\n")
    return "\n".join(out)


def strategy_stats(days: dict[str, DayRow], initial: float) -> dict[str, float]:
    """Aggregate numbers used in both the report doc and the README table."""
    if not days:
        return {"equity": initial, "pnl": 0.0, "pnl_pct": 0.0, "days": 0.0,
                "green": 0.0, "red": 0.0, "dd": 0.0, "fees": 0.0}
    last = days[sorted(days)[-1]]
    total = last.equity - initial
    return {
        "equity": last.equity,
        "pnl": total,
        "pnl_pct": (total / initial * 100.0) if initial else 0.0,
        "days": float(len(days)),
        "green": float(sum(1 for d in days.values() if d.pnl > 0.005)),
        "red": float(sum(1 for d in days.values() if d.pnl < -0.005)),
        "dd": max_drawdown(days),
        "fees": sum(d.fees for d in days.values()),
    }


def strategy_section(out_dir: Path, meta: dict[str, Any]) -> tuple[str, dict[str, float]]:
    """Markdown section for one strategy dir + its summary-table numbers."""
    strategy = meta.get("strategy") or out_dir.name
    label = STRATEGY_LABEL.get(strategy, "?")
    initial = float(meta.get("initial_equity") or 0)
    days = load_days(out_dir, initial)
    lots = load_closed_lots(out_dir, strategy)
    realized = sum(l["pnl"] for l in lots)
    lot_wins = sum(1 for l in lots if l["pnl"] > 0.005)
    st = strategy_stats(days, initial)
    lead = [
        f"## Strategy {label} — `{strategy}`",
        "",
        f"_Initial equity {_money(initial)}. Start "
        f"{(meta.get('created_at') or '?')[:10]}._",
        "",
        f"Equity **{_money(st['equity'])}** · P&L **{_money(st['pnl'], sign=True)}** "
        f"({_pct(st['pnl_pct'])}) · trading days **{st['days']:.0f}** "
        f"(green {st['green']:.0f} / red {st['red']:.0f}) · "
        f"max drawdown **{_pct(st['dd'])}** · fees {_money(st['fees'])}",
        "",
    ]
    if lots:
        lead += [
            f"- Closed lots: **{len(lots)}** (win rate {lot_wins / len(lots) * 100.0:.0f}%)",
            f"- Realized PnL from closed lots: **{_money(realized, sign=True)}** "
            "(approximation: sign × (close − entry) × contracts × contract_value)",
        ]
    else:
        lead.append("- Closed lots: 0")
    if days:
        last_day = days[sorted(days)[-1]]
        lead.append(f"- Open unrealized PnL: **{_money(last_day.unrealized, sign=True)}**")
        lead.append(f"- Open positions: "
                    + (", ".join(f"`{s} {'+' if v > 0 else ''}{v}`" for s, v in sorted(last_day.positions.items()))
                       or "none"))
    lead += [
        "_(Option P&L, when applicable, is an approximate Black-Scholes model.)_",
        "",
        "### PnL calendar",
        "",
    ]
    section = "\n".join(lead) + calendar_markdown(days) + "\n"
    section += "### Daily stats (last 30 recorded days)\n\n" + daily_stats_table(days) + "\n"
    row = {**{k: v for k, v in st.items()}, "strategy": strategy, "label": label}
    return section, row


def discover(out_dir: Path) -> list[Path]:
    """Strategy journal dirs under <out-dir>/<strategy>/ (each with meta.json)."""
    if not out_dir.exists():
        return []
    found = sorted(
        p for p in out_dir.iterdir()
        if p.is_dir() and (p / "meta.json").exists()
    )
    return found


def summary_table(rows: list[dict[str, Any]]) -> str:
    out = [
        "| Strategy | Equity | P&L | P&L % | Days (G/R) | Max DD | Fees |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        name = f"`{r['strategy']}` ({r['label']})"
        out.append(
            f"| {name} | {_money(r['equity'])} | {_money(r['pnl'], sign=True)} "
            f"| {_pct(r['pnl_pct'])} | {r['days']:.0f} ({r['green']:.0f}/{r['red']:.0f}) "
            f"| {_pct(r['dd'])} | {_money(r['fees'])} |"
        )
    out.append("")
    return "\n".join(out)


def build_combined(out_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    dirs = discover(out_dir)
    if not dirs:
        raise SystemExit(
            f"no strategy journals under {out_dir}<strategy>/meta.json — "
            "run tools.paper_session first"
        )
    sections: list[str] = []
    rows: list[dict[str, Any]] = []
    for d in dirs:
        meta = json.loads((d / "meta.json").read_text())
        sec, row = strategy_section(d, meta)
        sections.append(sec)
        rows.append(row)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    head = [
        "# Paper-trading report (A + B)",
        "",
        "_Paper money for each strategy: simulated fills on real Delta market "
        "data; zero orders sent, no API keys used._",
        f"_Generated {now}._",
        "",
        summary_table(rows),
        "> Strategies trade *separate simulated accounts*; their numbers are "
        "independent (per-strategy equity, PnL, fees).",
        "",
        "---",
        "",
    ]
    return "\n".join(head) + "\n".join(sections), rows


def README_snippet(rows: list[dict[str, Any]]) -> str:
    body = [
        summary_table(rows),
        f"Full per-strategy **PnL calendar + daily stats** in "
        f"[paper/REPORT.md](paper/REPORT.md).",
        "",
        "> Simulated fills on real market data, separate account per strategy;"
        " no real-money orders are ever sent.",
    ]
    return "\n".join(body)


def inject_readme(readme_path: Path, snippet: str) -> bool:
    if not readme_path.exists():
        return False
    text = readme_path.read_text()
    i, j = text.find(README_START), text.find(README_END)
    if i == -1 or j == -1 or j < i:
        return False
    new = text[: i + len(README_START)] + "\n\n" + snippet.rstrip() + "\n\n" + text[j:]
    if new != text:
        readme_path.write_text(new)
        return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="paper PnL calendar + daily stats report (A/B)")
    ap.add_argument("--out-dir", default="paper",
                    help="root dir containing one sub-dir per strategy")
    ap.add_argument("--readme", default=None, help="also update README between markers")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out_dir = Path(args.out_dir)
    full, rows = build_combined(out_dir)
    report_path = out_dir / "REPORT.md"
    report_path.write_text(full)
    log.info("wrote %s for %d strategy section(s)", report_path, len(rows))
    if args.readme and inject_readme(Path(args.readme), README_snippet(rows)):
        log.info("updated %s paper summary", args.readme)


if __name__ == "__main__":
    main()
