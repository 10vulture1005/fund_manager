"""PortfolioManager (specs 2.2, 2.10): one owner of allocation + risk.

It owns:
- the Allocator (dynamic weights -> budget per sleeve per NEW lot only)
- global checks vs an AccountSnapshot: max_gross_notional_to_equity,
  max_margin_utilisation_pct, min_liq_distance_pct (from
  PositionSnapshot.liquidation_price vs mark) -> breach: stop all new lots
  + option sales, de-risk newest-first until restored
- max_daily_loss_pct / max_drawdown_pct -> flatten + halt flags
- the cross-margin requirement: the EA/executor actually READS the margin
  mode (infra layer); this class REACTS to the hourly flag marshalled in
  (`set_margin_mode`).

`risk_breached=True` blocks new lots; `flatten_requested` asks the strategy
to flatten all sleeves (active breach of loss/drawdown limits).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from core.models import AccountSnapshot
from strategy.all_in_one.allocation import Allocator, SLEEVES
from strategy.all_in_one.defaults import get


def _utc_date(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).date().isoformat()


@dataclass
class RiskState:
    dated: Optional[str] = None
    daily_loss: float = 0.0
    drawdown: float = 0.0
    gross_notional: float = 0.0
    margin_utilisation: float = 0.0
    min_liq_distance: Optional[float] = None
    breach: bool = False
    breach_reasons: list[str] = field(default_factory=list)
    flatten: bool = False
    flatten_reasons: list[str] = field(default_factory=list)
    halted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "daily_loss": self.daily_loss,
            "drawdown": self.drawdown,
            "gross_notional": self.gross_notional,
            "margin_utilisation": self.margin_utilisation,
            "min_liq_distance": self.min_liq_distance,
            "breach": self.breach,
            "breach_reasons": self.breach_reasons,
            "flatten": self.flatten,
            "flatten_reasons": self.flatten_reasons,
            "halted": self.halted,
        }


class PortfolioManager:
    algo = "aio"

    def __init__(self, cfg: dict, allocator: Allocator, events: Any) -> None:
        self.cfg = cfg
        self.allocator = allocator
        self.events = events
        # limits (spec 2.10, mandatory because of cross margin)
        self.max_gross = float(get(cfg, "risk.max_gross_notional_to_equity", 3.0))
        self.max_margin_util = float(get(cfg, "risk.max_margin_utilisation_pct", 50)) / 100.0
        self.min_liq_distance = float(get(cfg, "risk.min_liq_distance_pct", 25)) / 100.0
        self.max_daily_loss = float(get(cfg, "risk.max_daily_loss_pct", 3)) / 100.0
        self.max_drawdown = float(get(cfg, "risk.max_drawdown_pct", 12)) / 100.0

        self.margin_mode_ok: Optional[bool] = None
        self.halt_source: Optional[str] = None   # "risk" auto-clears | manual stays
        self.risk_breached = False
        self.breach_detail: list[str] = []
        self.flatten_requested = False
        self.flatten_reasons: list[str] = []
        self.halted = False
        self.halt_reason: Optional[str] = None
        self.sleeve_halted: dict[str, str] = {}
        self.equity_peak: Optional[float] = None
        self.day_anchor: Optional[str] = None
        self.day_start_equity: Optional[float] = None
        self.state = RiskState()
        self.high_vol_sleeves: set[str] = set()

    # ------------------------------------------------------------- reactive
    def set_margin_mode(self, ok: bool, detail: str = "") -> None:
        """Hourly cross-margin check (infra reads the actual value)."""
        changed = ok is not self.margin_mode_ok
        self.margin_mode_ok = ok
        if not ok:
            self.events_event(
                "margin_mode", f"cross margin NOT confirmed: {detail or 'unknown'}",
                kind_ev="halt",
            )
        elif changed:
            self.events_event("margin_mode", "cross margin confirmed")

    def events_event(self, kind: str, message: str, kind_ev: Any = None,
                     features: Optional[dict] = None) -> None:
        try:
            self.events.log(kind=str(kind_ev or "info"), message=message,
                            features=features or {},
                            algo=f"{self.algo}.{kind}")
        except Exception:
            pass

    def halt_sleeve(self, sleeve: str, reason: str) -> None:
        self.sleeve_halted[sleeve] = reason
        self.events_event("reconcile", f"HALT sleeve {sleeve}: {reason}",
                          kind_ev="halt")

    def halt(self, reason: str, flatten: bool = False) -> None:
        self.halted = True
        self.halt_reason = reason
        if self.halt_source is None:
            self.halt_source = "risk"
        if flatten:
            self.flatten_requested = True
        self.events_event("halt", reason, kind_ev="halt",
                          features={"flatten": flatten})

    # ------------------------------------------------------------- gateways
    def global_block_new(self) -> Optional[str]:
        if self.margin_mode_ok is not True:
            return "cross_margin_not_confirmed"
        if self.halted:
            return f"halted ({self.halt_reason})"
        if self.risk_breached:
            return f"risk breach ({'; '.join(self.breach_detail)})"
        return None

    def allow_new_lot(self, sleeve: str) -> tuple[bool, str]:
        block = self.global_block_new()
        if block:
            return False, block
        if sleeve in self.sleeve_halted:
            return False, f"sleeve_halted ({self.sleeve_halted[sleeve]})"
        return True, "ok"

    def allow_option_sale(self, sleeve: str) -> tuple[bool, str]:
        return self.allow_new_lot(sleeve)

    def budget_for_new_lot(self, sleeve: str, max_lots: int, high_vol: bool = False,
                           n_symbols: int = 1) -> float:
        if high_vol:
            self.high_vol_sleeves.add(sleeve)
        else:
            self.high_vol_sleeves.discard(sleeve)
        return self.allocator.budget_for_new_lot(
            sleeve, max_lots=max_lots, high_vol=high_vol, n_symbols=n_symbols
        )

    # ---------------------------------------------------------------- risk
    def update(self, account: AccountSnapshot, now: Optional[datetime] = None) -> RiskState:
        """Recompute the global risk state from an AccountSnapshot."""
        now = now or datetime.now(timezone.utc)
        equity = float(account.equity or 0)
        if self.equity_peak is None:
            self.equity_peak = equity
        else:
            self.equity_peak = max(self.equity_peak, equity)
        # daily loss vs the UTC-day anchor (spec 2.10)
        day = _utc_date(now)
        if self.day_anchor != day:
            self.day_anchor = day
            self.day_start_equity = equity
        daily_loss = 0.0
        if self.day_start_equity and self.day_start_equity > 0:
            daily_loss = (self.day_start_equity - equity) / self.day_start_equity
        drawdown = 0.0
        if self.equity_peak and self.equity_peak > 0:
            drawdown = (self.equity_peak - equity) / self.equity_peak

        # gross notional of the (shared, cross-margined) book
        gross = 0.0
        min_liq: Optional[float] = None
        margin_util = 0.0
        if equity > 0:
            margin_util = float(account.used_margin or 0) / equity
        for pos in account.positions:
            size = abs(int(pos.net_size))
            if size == 0:
                continue
            mark = float(pos.mark_price) if pos.mark_price is not None else (
                float(pos.entry_price) if pos.entry_price is not None else 0.0
            )
            gross += size * mark
            if pos.mark_price and pos.liquidation_price:
                lp = float(pos.liquidation_price)
                mk = float(pos.mark_price)
                if lp > 0 and mk > 0:
                    d = abs(mk - lp) / mk
                    min_liq = d if min_liq is None else min(min_liq, d)
        gross /= equity if equity > 0 else 1.0

        st = RiskState(dated=day, daily_loss=daily_loss, drawdown=drawdown,
                       gross_notional=gross, margin_utilisation=margin_util,
                       min_liq_distance=min_liq)
        reasons: list[str] = []
        if gross > self.max_gross:
            reasons.append(f"gross_notional {gross:.2f} > {self.max_gross}")
        if margin_util > self.max_margin_util:
            reasons.append(f"margin utilisation {margin_util:.2%} > {self.max_margin_util:.0%}")
        if min_liq is not None and min_liq < self.min_liq_distance:
            reasons.append(f"liq distance {min_liq:.2%} < {self.min_liq_distance:.0%}")
        st.breach_reasons = reasons
        st.breach = bool(reasons)
        flatten_reasons: list[str] = []
        if daily_loss >= self.max_daily_loss:
            flatten_reasons.append(f"daily loss {daily_loss:.2%} >= {self.max_daily_loss:.0%}")
        if drawdown >= self.max_drawdown:
            flatten_reasons.append(f"drawdown {drawdown:.2%} >= {self.max_drawdown:.0%}")
        st.flatten_reasons = flatten_reasons
        st.flatten = bool(flatten_reasons)
        st.halted = self.halted

        prev_breach, prev_fl = self.risk_breached, self.flatten_requested
        self.risk_breached = st.breach
        self.breach_detail = list(reasons)
        if st.flatten and not prev_fl:
            self.halt("; ".join(flatten_reasons), flatten=True)
        self.flatten_requested = prev_fl or st.flatten
        if st.breach and not prev_breach:
            self.events_event("risk_veto",
                              "risk breach -> stop new lots + option sales, de-risk newest-first",
                              kind_ev="risk_veto", features={"reasons": reasons})
        if not (st.breach or st.flatten):
            if prev_breach or prev_fl or (
                self.halted and self.halt_source == "risk"
            ):
                self.risk_breached = False
                self.breach_detail = []
                self.flatten_requested = False
                if self.halted and self.halt_source == "risk":
                    self.halted = False
                    self.halt_reason = None
                    self.halt_source = None
                self.events_event("risk_veto",
                                  "risk restored; new lots allowed again")
        if st.breach or st.flatten:
            st.halted = self.halted
        self.state = st
        return st

    # --------------------------------------------------------------- de-risk
    async def derisk_newest_first(
        self, sleeves: list[Any], max_steps: int = 10,
        on_done: Optional[Any] = None,
    ) -> list[int]:
        """De-risk while in breach: close the newest lots first (spec 2.10).

        Sleeves expose `lot_exposure_usd()` and `derisk_step()`; each step
        closes the newest lot of the sleeve with the largest exposure."""
        if not self.risk_breached:
            return []
        closed: list[int] = []
        for _ in range(max_steps):
            if not self.risk_breached:
                break
            ranked = sorted(
                [s for s in sleeves if s.lot_exposure_usd() > 0],
                key=lambda s: s.lot_exposure_usd(), reverse=True,
            )
            if not ranked:
                break
            step_closed = await ranked[0].derisk_step()
            if not step_closed:
                break          # market moves against us; stop,
            closed.extend(step_closed)
            if on_done is not None:
                # caller drives any re-check with a fresh account snapshot
                await on_done()
                if not self.risk_breached:
                    break
        return closed

    def clear_flatten(self) -> None:
        self.flatten_requested = False

    def status(self) -> dict[str, Any]:
        return {
            "margin_mode_ok": self.margin_mode_ok,
            "risk_breached": self.risk_breached,
            "breach_detail": list(self.breach_detail),
            "flatten_requested": self.flatten_requested,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "sleeve_halted": dict(self.sleeve_halted),
            "equity_peak": self.equity_peak,
            "day_start_equity": self.day_start_equity,
            "risk": self.state.as_dict(),
            "weights": dict(self.allocator.weights),
            "budgets": dict(self.allocator.allocation().budgets),
            "high_vol_sleeves": sorted(self.high_vol_sleeves),
        }
