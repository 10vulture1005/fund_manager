"""Deadman switch / heartbeat client for Delta Exchange India (spec 1.9).

Delta provides Heartbeat Management endpoints: the bot registers a heartbeat
with an interval and must acknowledge it on a timer; if it stops, the exchange
fails over and cancels open orders. Configurable, default ON in prod.

TODO(VERIFY): the prompt summary names no exact paths/keys; the docs section
"Deadman Switch → Implementation Guidelines / Error Codes" must be read before
prod use. Currently implemented as:
  - PUT    /v2/heartbeat        body {"interval": <seconds>}   (register/ack)
  - GET    /v2/heartbeat        (status)
  - DELETE /v2/heartbeat        (unregister, graceful shutdown)
If the live docs differ (e.g. POST /heartbeat / deactivate endpoints), adjust
`_put heartbeat` calls only — `DeadmanSwitch` encapsulates the rest.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from .rest import DeltaAPIError, DeltaRestClient

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 120


class DeadmanSwitch:
    """Register + acknowledge the exchange heartbeat on a timer (spec 1.9).

    Graceful degradation: when `enabled` is False or no API key is configured,
    the switch stays inert (start() is a no-op) so unauthenticated / paper /
    backtest runs are unaffected.
    """

    def __init__(
        self,
        rest: DeltaRestClient,
        interval_s: int = DEFAULT_INTERVAL_S,
        grace_period_s: int | None = None,
        enabled: bool = True,
        has_credentials: bool = False,
        ack_fraction: float = 0.25,  # ack well inside the failure window
    ) -> None:
        self.rest = rest
        self.interval_s = int(interval_s)
        self.grace_period_s = grace_period_s
        self.enabled = enabled and has_credentials
        self.ack_every = max(1.0, self.interval_s * ack_fraction)
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self.last_ack: float = 0.0

    # ------------------------------------------------------------------- REST
    # TODO(VERIFY): exact endpoint paths & payload keys for the heartbeat API.
    async def _put_heartbeat(self) -> dict:
        body: dict = {"interval": self.interval_s}
        if self.grace_period_s:
            body["grace_period"] = self.grace_period_s
        result, _ = await self.rest._request(
            "PUT", "/heartbeat", body=body, weight=1
        )
        return result

    async def get_status(self) -> dict | None:
        try:
            result, _ = await self.rest._request("GET", "/heartbeat", weight=1)
            return result
        except DeltaAPIError as exc:
            logger.warning("heartbeat status failed: %s", exc.code)
            return None

    async def unregister(self) -> bool:
        """Remove the heartbeat (call on graceful shutdown; orders stay live)."""
        if not self.enabled:
            return False
        try:
            await self.rest._request("DELETE", "/heartbeat", weight=1)
            return True
        except DeltaAPIError:
            logger.exception("heartbeat unregister failed")
            return False

    # ----------------------------------------------------------------- loop
    async def start(self, on_ack: Callable[[], Awaitable[None]] | None = None) -> bool:
        """Register the heartbeat and spawn the acknowledgement timer."""
        if not self.enabled:
            logger.info("deadman switch disabled (off or no credentials)")
            return False
        try:
            await self._put_heartbeat()
        except DeltaAPIError as exc:
            logger.error("heartbeat registration failed (%s); proceeding WITHOUT "
                         "deadman protection", exc.code)
            return False
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(on_ack))
        logger.info("deadman switch registered: interval=%ss ack~%.0fs",
                    self.interval_s, self.ack_every)
        return True

    async def _loop(self, on_ack: Callable[[], Awaitable[None]] | None) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(self.ack_every)
            try:
                await self._put_heartbeat()  # ack == re-PUT interval
                self.last_ack = asyncio.get_running_loop().time()
                if on_ack is not None:
                    await on_ack()
            except DeltaAPIError as exc:
                logger.error("heartbeat ack failed: %s", exc.code)  # SIGTERM-style retry next tick
            except Exception:
                logger.exception("unexpected heartbeat ack failure")

    async def stop(self, unregister: bool = True) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if unregister and self.enabled:
            await self.unregister()

    @property
    def healthy(self) -> bool:
        return self.enabled and self._task is not None and not self._task.done()
