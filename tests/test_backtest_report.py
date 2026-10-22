import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, "src")

from backtest.report import TradeRecord, equity_stats, trade_stats, write_report
from backtest.engine import run_backtest  # noqa


def series(n=600):
    import numpy as np
    import pandas as pd

    idx = pd.date_range("2024-01-01", periods=n, freq="h")
    vals = 10000 * np.cumprod(1 + np.sin(np.arange(n) / 11) / 900)
    return pd.Series(vals, index=idx)


def test_equity_stats_sane():
    s = equity_stats(series())
    assert s["total_return"] != 0.0
    assert s["max_drawdown"] <= 0.0
    assert "sharpe" in s and "sortino" in s and "cagr" in s


def test_trade_stats():
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
    trades = [
        TradeRecord("a", "X", ts, ts + timedelta(hours=2), 120.0, "tp", 2, 500.0, -0.01),
        TradeRecord("a", "X", ts + timedelta(hours=5), ts + timedelta(hours=9), -40.0, "sl", 3, 400.0, -0.05),
        TradeRecord("a", "Y", ts + timedelta(hours=10), ts + timedelta(hours=13), 30.0, "tp", 4, 750.0, -0.02),
    ]
    st = trade_stats(trades)
    assert st["n_trades"] == 3
    assert st["win_rate"] == pytest.approx(2 / 3)
    assert st["profit_factor"] == pytest.approx(150 / 40)
    assert st["exit_reasons"] == {"tp": 2, "sl": 1}
    assert st["worst_adverse_excursion"] == pytest.approx(-0.05)


def test_write_report(tmp_path):
    eq = [{"ts": t.isoformat(), "equity": e, "used_margin": 0.0, "gross_notional": 0.0}
          for t, e in zip(series().index, series().values)]
    summary = {"initial_equity": 10000.0, "final_equity": eq[-1]["equity"], "fills": 10}
    rep = write_report(summary, [], eq, tmp_path, title="t")
    assert (tmp_path / "equity_curve.csv").exists()
    assert (tmp_path / "report.json").exists()
    assert "monthly_returns" in rep
