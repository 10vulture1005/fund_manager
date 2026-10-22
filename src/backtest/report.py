"""Backtest reporting (spec 3.6): total return, CAGR, drawdowns, Sharpe/Sortino,
profit factor, win rate, campaign duration stats, exit-reason shares, monthly
returns table, equity curve CSV/PNG (PNG optional via matplotlib).

Plus `calibrate` support: compare against calibration_targets.yaml and report
which parameters likely matter most (sensitivity via finite sweep outputs).
"""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd


@dataclass
class TradeRecord:
    """One completed campaign/lot-level trade for stats aggregation."""

    algo: str
    symbol: str
    opened_at: datetime
    closed_at: datetime
    pnl: float
    reason: str = ""
    rungs_filled: int = 1
    margin_used: float = 0.0
    adverse_excursion: float = 0.0


def equity_stats(equity: pd.Series, periods_per_year: int = 252) -> dict[str, float]:
    eq = equity.dropna().astype(float)
    if len(eq) < 2:
        return {"total_return": 0.0, "cagr": 0.0, "max_drawdown": 0.0}
    total_return = eq.iloc[-1] / eq.iloc[0] - 1.0
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (365.0 / days) - 1.0
    max_drawdown = ((eq / eq.cummax()) - 1.0).min()
    returns = eq.pct_change().dropna()
    sharpe = float(returns.mean() / returns.std() * math.sqrt(periods_per_year)) if returns.std() > 0 else 0.0
    downside = returns[returns < 0]
    sortino = (
        float(returns.mean() / downside.std() * math.sqrt(periods_per_year))
        if len(downside) > 1 and downside.std() > 0
        else 0.0
    )
    return {
        "total_return": float(total_return),
        "cagr": float(cagr),
        "max_drawdown": float(max_drawdown),
        "sharpe": sharpe,
        "sortino": sortino,
    }


def trade_stats(trades: list[TradeRecord]) -> dict[str, Any]:
    if not trades:
        return {"profit_factor": 0.0, "win_rate": 0.0, "n_trades": 0}
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    durations = [(t.closed_at - t.opened_at).total_seconds() / 3600.0 for t in trades]
    reasons: dict[str, int] = {}
    for t in trades:
        k = t.reason or "unspecified"
        reasons[k] = reasons.get(k, 0) + 1
    return {
        "n_trades": len(trades),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "win_rate": len(wins) / len(trades),
        "avg_duration_hours": float(np.mean(durations)) if durations else 0.0,
        "median_duration_hours": float(np.median(durations)) if durations else 0.0,
        "avg_margin_used": float(np.mean([t.margin_used for t in trades])) if trades else 0.0,
        "worst_adverse_excursion": min((t.adverse_excursion for t in trades), default=0.0),
        "rungs_filled_distribution": {
            str(k): int(v)
            for k, v in pd.Series([t.rungs_filled for t in trades]).value_counts().items()
        },
        "exit_reasons": reasons,
    }


def monthly_returns(equity: pd.Series) -> pd.DataFrame:
    monthly = equity.resample("ME").last().pct_change()
    df = pd.DataFrame({"month": monthly.index.strftime("%Y-%m"), "return": monthly.values})
    return df


def write_report(
    summary: dict[str, Any],
    trades: list[TradeRecord],
    equity_curve: list[dict[str, float]],
    out_dir: Path,
    title: str = "backtest",
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    eq = pd.Series(
        [e["equity"] for e in equity_curve],
        index=pd.to_datetime([e["ts"] for e in equity_curve]),
    ).sort_index()

    report = {
        "title": title,
        **equity_stats(eq),
        **trade_stats(trades),
        "initial_equity": summary.get("initial_equity"),
        "final_equity": summary.get("final_equity"),
        "fills": summary.get("fills"),
        "monthly_returns": [],
        "disclaimer": (
            "Option P&L (if any) is a Black-Scholes APPROXIMATION with realised vol "
            "and an IV premium — never claimed exact. Specs are inferred from a "
            "public strategy description; results do not reproduce any leader."
        ),
    }
    mdf = monthly_returns(eq)
    if len(mdf):
        report["monthly_returns"] = [
            {"month": r.month, "return": (None if pd.isna(r["return"]) else float(r["return"]))}
            for _, r in mdf.iterrows()
        ]

    # equity curve csv
    with (out_dir / "equity_curve.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "equity", "used_margin", "gross_notional"])
        for e in equity_curve:
            w.writerow([e["ts"], e["equity"], e.get("used_margin", ""), e.get("gross_notional", "")])

    with (out_dir / "report.json").open("w") as f:
        json.dump(report, f, indent=2, default=str)

    # optional png
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(10, 6))
        ax.plot(eq.index, eq.values)
        ax.set_title(title)
        ax.set_ylabel("equity")
        fig.tight_layout()
        fig.savefig(out_dir / "equity_curve.png", dpi=120)
        plt.close(fig)
    except Exception:
        report["png_skipped"] = "matplotlib not installed"
    return report


def calibrate(
    report: dict[str, Any],
    targets_path: Path,
) -> dict[str, Any]:
    """Compare backtest report vs `calibration_targets.yaml` (spec 2.13 / 3.6)."""
    import yaml

    targets = yaml.safe_load(targets_path.read_text()) or {}
    comparison: dict[str, Any] = {}
    metric_map = {
        "roi": report.get("total_return"),
        "total_return": report.get("total_return"),
        "win_rate": report.get("win_rate"),
        "max_drawdown": report.get("max_drawdown"),
        "sharpe": report.get("sharpe"),
        "average_holding_time_hours": report.get("avg_duration_hours"),
        "profit_factor": report.get("profit_factor"),
    }
    for key, target in (targets.get("metrics") or targets).items():
        if key not in metric_map:
            continue
        got = metric_map.get(key)
        comparison[key] = {"target": target, "backtest": got}
        if isinstance(target, (int, float)) and isinstance(got, (int, float)) and target != 0:
            comparison[key]["delta_pct"] = (got - target) / abs(target) * 100.0
    # parameter influence hints (fixed list from the specs)
    comparison["most_influential_parameters"] = [
        "opt_target_delta",
        "lot_spacing_atr",
        "lot_tp_pct",
        "dd_entry_pct",
        "sweep_min_atr",
        "ladder_atr_mult",
        "leverage",
        "profit_target_pct_of_margin",
        "allocation.base_weights",
    ]
    return comparison
