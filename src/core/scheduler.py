"""Simple asyncio job scheduler for the fund manager (prompt A/B 2.11).

Three job kinds:
- interval: run once per N seconds (the 1-minute housekeeping loop)
- daily_at_utc("00:00"): run at the next (then every) UTC midnight
- on_candle(resolutions): triggered by the host pushing CLOSED candles via
  `push_candle` — strategies get on_candle_close callbacks (spec 2.3/2.3).

Graceful shutdown: `stop()` cancels all task loops and awaits them.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Iterable, Sequence

from core.models import Candle

logger = logging.getLogger(__name__)


@dataclass
class Job:
    name: str
    coro_fn: Callable[[], Awaitable[None]]  # no-arg async callable
    kind: str = "interval"                   # interval | daily | candle
    interval: float = 60.0
    at: str = "00:00"                        # "HH:MM" UTC for daily jobs
    resolutions: tuple[str, ...] = ()        # for on_candle jobs
    task: asyncio.Task | None = field(default=None, repr=False)


class Scheduler:
    """Run-loop based scheduler with fail-soft job execution."""

    def __init__(self, name: str = "scheduler") -> None:
        self.name = name
        self.jobs: list[Job] = []
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------ registration
    def add_job(
        self,
        name: str,
        coro_fn: Callable,
        interval: float | None = None,
        daily_at_utc: str | None = None,
        on_candle: Sequence[str] | Iterable[str] | None = None,
    ) -> Job:
        """Exactly one scheduling dimension per job (interval XOR daily XOR candles)."""
        provided = [x for x in (interval, daily_at_utc, on_candle) if x is not None]
        if len(provided) != 1:
            raise ValueError("add_job takes exactly one of interval / daily_at_utc / on_candle")
        if interval is not None:
            job = Job(name, coro_fn, kind="interval", interval=float(interval))
        elif daily_at_utc is not None:
            job = Job(name, coro_fn, kind="daily", at=daily_at_utc)
        else:
            resolutions = tuple(on_candle)  # type: ignore[arg-type]
            if not resolutions:
                raise ValueError("on_candle needs at least one resolution")
            job = Job(name, coro_fn, kind="candle", resolutions=resolutions)
        self.jobs.append(job)
        return job

    # ---------------------------------------------------------------- candles
    async def push_candle(self, candle: Candle) -> None:
        """Fan a CLOSED candle out to the on_candle jobs of its resolution.
        Jobs run concurrently; exceptions are logged, not raised (fail-soft)."""
        if not candle.closed:
            return  # closed candles only (spec 3.6 no-lookahead)
        for job in self.jobs:
            if job.kind == "candle" and candle.resolution in job.resolutions:
                await self._run_once(job, candle.resolution)

    # ------------------------------------------------------------------- loop
    @staticmethod
    def _next_daily_run(at: str, now: datetime) -> datetime:
        hh, mm = (int(x) for x in at.split(":"))
        today_once = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return today_once if today_once > now else today_once + timedelta(days=1)

    async def _run_once(self, job: Job, *args: object) -> None:
        try:
            await job.coro_fn(*args)
        except Exception:
            logger.exception("job %s failed (continuing)", job.name)

    async def run(self) -> None:
        """Main loop; returns after stop() (graceful shutdown)."""
        self._stopping.clear()
        for job in self.jobs:
            if job.kind != "interval":
                continue
            job.task = asyncio.create_task(self._interval_loop(job))

        daily_iters: dict[str, asyncio.Task | None] = {
            job.name: (asyncio.create_task(self._daily_loop(job)) if job.kind == "daily" else None)
            for job in self.jobs
        }
        try:
            await self._stopping.wait()
        except asyncio.CancelledError:
            pass

    async def _interval_loop(self, job: Job) -> None:
        # run immediately, then on cadence
        await self._run_once(job)
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=job.interval)
                return
            except asyncio.TimeoutError:
                pass
            await self._run_once(job)

    async def _daily_loop(self, job: Job) -> None:
        while not self._stopping.is_set():
            now = datetime.now(timezone.utc)
            run_at = self._next_daily_run(job.at, now)
            wait_s = (run_at - now).total_seconds()
            logger.info("daily job %s next run in %.0fs at %s", job.name, wait_s, run_at)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=wait_s)
                return
            except asyncio.TimeoutError:
                pass
            await self._run_once(job)

    # ------------------------------------------------------------- shutdown
    def stop(self) -> None:
        self._stopping.set()

    async def run_until_stopped(self) -> None:
        await self.run()

    async def graceful_shutdown(self) -> None:
        self.stop()
        tasks = [j.task for j in self.jobs if j.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs = []
