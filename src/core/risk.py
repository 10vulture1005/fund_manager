"""GlobalRiskMonitor — mandatory static risk checks (spec 3.4, prompt B 2.10).

NO network: callers pass an `AccountSnapshot` from exec.account() and mark
prices; the monitor keeps its own equity curve in SQLite (peak equity,
day-open equity) so drawdown / daily-loss checks survive restarts (spec 3.3).

Mandatory checks (defaults):
- max_daily_loss_pct (3): equity down from the UTC-day-open equity → no new
  campaigns/lots until the next 00:00 UTC. FRIDAY ROLLOVER: a daily-loss block
  that is still active on/after Friday 00:00 UTC persists through the weekend
  until Monday 00:00 UTC (no "next midnight" for gapped weekend markets).
- max_drawdown_pct (12): equity below peak × (1 − pct) → halt + flatten.
- max_margin_utilisation_pct (50): used_margin / equity.
- max_gross_notional_to_equity (3): Σ |net_size × price| / equity.
- min_liq_distance_pct (25): |mark − liquidation_price| / mark per position.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .state import DeltaSchema

DEFAULTS: dict[str, float] = {
    "max_daily_loss_pct": 3.0,
    "max_drawdown_pct": 12.0,
    "max_margin_utilisation_pct": 50.0,
    "max_gross_notional_to_equity": 3.0,
    "min_liq_distance_pct": 25.0,
}

TABLE_EQ = "CREATE TABLE IF NOT EXISTS {p}risk_equity (ts TEXT NOT NULL, equity REAL NOT NULL)"
TABLE_STATE = "CREATE TABLE IF NOT EXISTS {p}risk_state (key TEXT PRIMARY KEY, value TEXT)"


@dataclass
class RiskVerdict:
    """Result of one assess() pass; `halt` blocks new sizing, `flatten` is
    the "kill and close" tier (spec 3.4)."""

    ok: bool = True
    veto_new: bool = False     # no new lots/campaigns/options
    halt: bool = False         # stop trading on this account
    flatten: bool = False      # politely close everything (reduce_only)
    breaches: list[str] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)


class GlobalRiskMonitor:
    """Static bool checks + equity-curve persistence + Friday rollover.

    Construct with a config dict, e.g.:
        GlobalRiskMonitor({"risk": {"max_daily_loss_pct": 3, ...}}, db_path)
    Flat keys are also accepted for convenience.
    """

    def __init__(self, config: dict[str, Any] | None = None, db_path: str = "state.db",
                 prefix: str = "") -> None:
        cfg = dict(config or {})
        nested = cfg.get("risk") or cfg.get("risk_limits") or {}
        effective: dict[str, Any] = {**DEFAULTS, **cfg, **nested}
        for key, default in DEFAULTS.items():
            setattr(self, key, float(effective.get(key, default)))
        self._db_path = db_path
        self._prefix = prefix
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = DeltaSchema(db_path, table_prefix=prefix)
        self.db.exec(TABLE_EQ.format(p=prefix))
        self.db.exec(TABLE_STATE.format(p=prefix))
        self._load_state()

    # ------------------------------------------------------------ persistence
    def _load_state(self) -> None:
        rows = self.db.query(f"SELECT key, value FROM {self._t_state}")
        state = {r["key"]: r["value"] for r in rows}
        try:
            def f(key: str) -> Decimal | None:
                v = state.get(key)
                return Decimal(str(v)) if v is not None else None

            self._peak_equity: Decimal | None = f("peak_equity")
            self._day_open_equity: Decimal | None = f("day_open_equity")
            self._day_date: str | None = state.get("day_date")
            self._blocked_until: str | None = state.get("blocked_until")
        except (ArithmeticError, ValueError, TypeError, KeyError):
            self._peak_equity = None

    def _t(self, name: str) -> str:
        return self._prefix + name

    @property
    def _t_state(self) -> str:
        return self._t("risk_state")

    def _save_state(self) -> None:
        pairs = {
            "peak_equity": self._peak_equity,
            "day_open_equity": self._day_open_equity,
            "day_date": self._day_date,
            "blocked_until": self._blocked_until,
        }
        with self.db.transaction() as conn:
            for key, value in pairs.items():
                conn.execute(
                    f"INSERT OR REPLACE INTO {self._t_state}(key, value) VALUES (?,?)",
                    (key, str(value) if value is not None else None),
                )

    def record_equity(self, equity: Decimal, ts: datetime | None = None) -> None:
        """Append one equity point and keep peak / day-open book-keeping fresh
        (equity-curve persistence, spec 3.4)."""
        ts = ts or datetime.now(timezone.utc)
        date = ts.strftime("%Y-%m-%d")
        self.db.exec(
            f"INSERT INTO {self._t('risk_equity')}(ts, equity) VALUES (?,?)",
            (ts.isoformat(timespec="milliseconds"), float(equity)),
        )
        if self._day_date != date or self._day_open_equity is None:
            self._day_open_equity = equity
            self._day_date = date
        self._peak_equity = max(self._peak_equity or equity, equity)
        self._save_state()

    # --------------------------------------------------------------- helpers
    def _block_deadline(self, now: datetime) -> str:
        """Next 00:00 UTC after `now`; if `now` is Friday (or a weekend day)
        the block survives the weekend and expires Monday 00:00 UTC (spec 2.11
        "no new campaigns until next 00:00 UTC" + weekend market gaps)."""
        one_day = timedelta(days=1)
        next_midnight = (now + one_day).replace(hour=0, minute=0, second=0, microsecond=0)
        if next_midnight <= now:
            next_midnight += one_day
        weekday = now.weekday()  # Mon=0..Sun=6
        if weekday == 4 or weekday >= 5:  # Fri / Sat / Sun
            days_ahead = 7 - weekday  # next Monday
            monday = (now + timedelta(days=days_ahead)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return monday.isoformat()
        return next_midnight.isoformat()

    def is_blocked_until(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if not self._blocked_until:
            return False
        deadline = datetime.fromisoformat(self._blocked_until)
        return now < deadline

    # ----------------------------------------------------------------- assess
    def assess(
        self,
        account: Any,  # core.models.AccountSnapshot
        mark_prices: dict[str, Decimal] | None = None,
        now: datetime | None = None,
    ) -> RiskVerdict:
        """Run all mandatory checks against one snapshot (no network, spec 3.4)."""
        verdict = RiskVerdict()
        now = now or datetime.now(timezone.utc)
        equity = account.equity if account else Decimal("0")
        if equity and equity > 0:
            self.record_equity(equity, now)

        breaches = verdict.breaches

        # 1) daily loss → veto new until next 00:00 UTC (Friday rollover aware)
        if self._blocked_until:
            try:
                if now < datetime.fromisoformat(self._blocked_until):
                    breaches.append("daily_loss_block_active")
                    verdict.veto_new = True
            except (ValueError, TypeError):
                pass
        day_open = self._day_open_equity
        if day_open is not None and day_open > 0 and equity is not None:
            loss_pct = float((day_open - equity) / day_open) * 100.0
            verdict.notes["daily_loss_pct"] = loss_pct
            if loss_pct >= self.max_daily_loss_pct:
                self._blocked_until = self._block_deadline(now)
                breaches.append(f"daily_loss_{loss_pct:.2f}%>={self.max_daily_loss_pct}%")
                verdict.veto_new = True
                self._save_state()

        # 2) max drawdown from peak → halt + flatten
        if self._peak_equity is not None and self._peak_equity > 0 and equity is not None:
            dd = float((self._peak_equity - equity) / self._peak_equity) * 100.0
            verdict.notes["drawdown_pct"] = dd
            if dd >= self.max_drawdown_pct:
                breaches.append(f"max_drawdown_{dd:.2f}%")
                verdict.veto_new = True
                verdict.halt = True
                verdict.flatten = True

        # 3) margin utilisation
        used = account.used_margin if account else Decimal("0")
        if equity and equity > 0 and used is not None:
            mu = float(used / equity) * 100.0
            verdict.notes["margin_util_pct"] = mu
            if mu > self.max_margin_utilisation_pct:
                breaches.append(f"margin_utilisation_{mu:.2f}%")
                verdict.veto_new = True

        # 4) gross notional / equity + 5) liq distance (per position)
        gross = Decimal("0")
        for pos in (account.positions if account else []):
            mark = (mark_prices or {}).get(pos.symbol) or pos.mark_price
            if mark is not None:
                gross += abs(Decimal(pos.net_size) * mark)
            if pos.liquidation_price is not None and mark is not None and mark > 0:
                distance = abs(mark - pos.liquidation_price) / mark * 100
                if distance < self.min_liq_distance_pct:
                    breaches.append(
                        f"liq_distance_{pos.symbol}_{distance:.1f}%<min"
                    )
                    verdict.veto_new = True
                    verdict.halt = True  # position is near liquidation: protect it
        if equity and equity > 0:
            ratio = float(gross / equity)
            verdict.notes["gross_notional_to_equity"] = ratio
            if ratio > self.max_gross_notional_to_equity:
                breaches.append(f"gross_notional_to_equity_{ratio:.2f}")
                verdict.veto_new = True

        verdict.ok = not breaches
        return verdict

    def reset_daily_block(self) -> None:
        self._blocked_until = None
        self._save_state()

    def snapshot(self) -> dict[str, Any]:
        return {
            "peak_equity": str(self._peak_equity) if self._peak_equity is not None else None,
            "day_open_equity": str(self._day_open_equity) if self._day_open_equity is not None else None,
            "day_date": self._day_date,
            "limits": {k: getattr(self, k) for k in DEFAULTS},
        }
