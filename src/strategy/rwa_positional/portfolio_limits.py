"""Portfolio-level limits (spec 2.11) and the gold risk bucket.

- max_concurrent_campaigns (3)
- max_total_margin_pct (40% of equity)
- max_daily_loss_pct (3%) -> no NEW campaigns until next 00:00 UTC
- max_drawdown_pct (10%) -> flatten + HALT signal
- liquidation-distance monitor: mark within 2 x ATR(4H) of the position's
  liquidation price -> cancel unfilled rungs + halve the campaign.
- Gold bucket: XAUTUSD/PAXGUSD share max_campaigns_per_bucket (1).

Owns in-memory state (peak equity, day anchor) seeded from `on_equity_tick`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class PortfolioState:
    equity: float = 0.0
    peak_equity: float = 0.0
    day_anchor_equity: float = 0.0
    day_anchor_date: Optional[str] = None
    halted: bool = False
    halt_reason: str = ""


class PortfolioLimits:
    def __init__(self, cfg: dict[str, Any]) -> None:
        self.max_concurrent = int(cfg.get("max_concurrent_campaigns", 3))
        self.max_total_margin_pct = float(cfg.get("max_total_margin_pct", 40.0))
        self.max_daily_loss_pct = float(cfg.get("max_daily_loss_pct", 3.0))
        self.max_drawdown_pct = float(cfg.get("max_drawdown_pct", 10.0))
        self.liq_atr_mult_4h = float(cfg.get("liquidation_atr_mult_4h", 2.0))
        self.buckets: dict[str, list[str]] = {
            "gold": list(cfg.get("gold_bucket_symbols", ["XAUTUSD", "PAXGUSD"])),
        }
        self.max_per_bucket = int(cfg.get("max_campaigns_per_bucket", 1))
        self.state = PortfolioState()

    # ---- equity ------------------------------------------------------------
    def on_equity_tick(self, equity: float, now: datetime | None = None) -> list[str]:
        """Update anchors; returns action strings, e.g. ["halt"] / ["new_day"]."""
        now = now or datetime.now(timezone.utc)
        actions: list[str] = []
        st = self.state
        if equity <= 0:
            return actions
        st.equity = equity
        st.peak_equity = max(st.peak_equity or equity, equity)
        today = now.astimezone(timezone.utc).date().isoformat()
        if st.day_anchor_date != today:
            st.day_anchor_date = today
            st.day_anchor_equity = equity
            actions.append("new_day")
        # drawdown control: peak -> current drop >= max_drawdown_pct -> halt
        if st.peak_equity > 0:
            dd = (st.peak_equity - equity) / st.peak_equity * 100.0
            if dd >= self.max_drawdown_pct and not st.halted:
                st.halted = True
                st.halt_reason = (
                    f"max_drawdown_pct {self.max_drawdown_pct}% breached "
                    f"(dd={dd:.2f}% from peak {st.peak_equity:.2f})"
                )
                actions.append("halt")
        return actions

    def daily_loss_breached(self, equity: float) -> bool:
        st = self.state
        anchor = st.day_anchor_equity
        if anchor <= 0:
            return False
        return (anchor - equity) / anchor * 100.0 >= self.max_daily_loss_pct

    # ---- admission of new campaigns ---------------------------------------
    def can_open_campaign(
        self,
        *,
        open_campaigns: list[dict[str, Any]] | int,
        equity: float,
        total_margin_used: float,
        symbol: str,
        now: datetime | None = None,
    ) -> tuple[bool, str]:
        now = now or datetime.now(timezone.utc)
        if self.state.halted:
            return False, f"halted: {self.state.halt_reason}"
        n = open_campaigns if isinstance(open_campaigns, int) else len(open_campaigns)
        if n >= self.max_concurrent:
            return False, f"max_concurrent_campaigns {self.max_concurrent} reached ({n})"
        if equity > 0:
            used_pct = total_margin_used / equity * 100.0
            if used_pct >= self.max_total_margin_pct:
                return False, f"max_total_margin_pct {self.max_total_margin_pct}% reached ({used_pct:.1f}%)"
        if self.daily_loss_breached(equity):
            return False, (
                f"max_daily_loss_pct {self.max_daily_loss_pct}% breached — "
                f"no new campaigns until next 00:00 UTC"
            )
        per_bucket = self.bucket_counts(
            open_campaigns if not isinstance(open_campaigns, int) else []
        )
        bucket = self.bucket_of(symbol)
        if per_bucket.get(bucket, 0) >= self.max_per_bucket:
            return False, (
                f"bucket '{bucket}' at max_campaigns_per_bucket={self.max_per_bucket}"
            )
        return True, "ok"

    def bucket_of(self, symbol: str) -> str:
        for name, syms in self.buckets.items():
            if symbol in syms:
                return name
        return f"single:{symbol}"

    def bucket_counts(
        self, open_campaigns: list[dict[str, Any]] | None
    ) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in open_campaigns or []:
            b = self.bucket_of(c["symbol"])
            out[b] = out.get(b, 0) + 1
        return out

    # ---- liquidation monitor ----------------------------------------------
    def liquidation_risk(
        self,
        position: Any,                 # PositionSnapshot
        mark: float,
        atr_4h: float,
    ) -> bool:
        if position is None or position.liquidation_price is None:
            return False
        if atr_4h <= 0:
            return False
        liq = float(position.liquidation_price)
        if position.net_size > 0:   # long: liq below mark
            return mark - liq <= self.liq_atr_mult_4h * atr_4h
        if position.net_size < 0:   # short: liq above mark
            return liq - mark <= self.liq_atr_mult_4h * atr_4h
        return False
