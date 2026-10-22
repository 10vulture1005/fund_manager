"""WebSocket feed client for Delta Exchange India (spec 1.8).

Public channels: candlesticks, ticker, ob_l1, mark_price, funding_rate,
product_updates, system_status.
Private channels: orders, positions, margins, user trades — gated behind an
authentication message per the docs' "Websocket Feed → Authenticating" section.

Behaviour and honesty notes:
- Reconnect = exponential backoff + jitter, then resubscribe everything, then
  fire the injectable `on_reconnect_full_resync` awaitable so the strategy
  layer can run its REST reconciliation before trading resumes.
- Dropped-connection detection: server heartbeats (docs show `{type:"heartbeat"}`
  frames); if none arrive within HEARTBEAT_TIMEOUT the socket is torn down and
  reconnected. TODO(VERIFY): exact heartbeat frame/interval on the India feed.
- The India socket host differs from Delta Global (spec 1.8). Defaults come
  from core.config.ApiConfig-derived URL passed by the caller.
- TODO(VERIFY): the private-channel authentication message exact shape
  (`{"type":"auth","api-key":..,"signature":..,"timestamp":..}`) and the exact
  signed string ("GET" + timestamp + "/live") must be confirmed against the
  live docs; URL and auth message marked below.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, Awaitable, Callable, Mapping, Sequence

import websockets

from .auth import AuthSigner, sign_request
from core.models import Candle, FillEvent, PositionSnapshot, Ticker

logger = logging.getLogger(__name__)

PUBLIC_CHANNELS = (
    "candlesticks", "ticker", "ob_l1", "mark_price", "funding_rate",
    "product_updates", "system_status",
)
PRIVATE_CHANNELS = ("orders", "positions", "margins", "v2_user_trades", "user_trades")
# TODO(VERIFY): docs list both "user_trades" and "v2/user_trades" (channel
# naming differs); subscribe handler accepts both.

HEARTBEAT_INTERVAL_S = 10.0       # TODO(VERIFY): docs heartbeat cadence
HEARTBEAT_TIMEOUT_S = 25.0
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0

AsyncCallback = Callable[
    [str, str, dict[str, Any]], Awaitable[None]
]  # (channel, symbol, payload)


class _Reconnect(Exception):
    pass


class DeltaWSClient:
    """Async context manager with awaitable per-(channel, symbol) callbacks."""

    def __init__(
        self,
        ws_url: str,
        signer: AuthSigner | None = None,
        auth_needed: bool = False,
        chunks: int = 1,
    ) -> None:
        self.url = ws_url
        self.signer = signer
        self.auth_needed = auth_needed
        self._subs: dict[str, list[str]] = {}     # channel -> symbols ("" = wildcard)
        self._cbs: dict[tuple[str, str], list[AsyncCallback]] = {}
        self._ws: Any = None
        self._task: asyncio.Task | None = None
        self._closing = False
        self._reconnect_hook: Callable[[], Awaitable[None]] | None = None
        self.last_heartbeat: float = 0.0
        self.is_reconnecting: bool = False  # true until resync hook passed (1.8)

    # ------------------------------------------------------------- registry
    def on(
        self,
        channel: str,
        symbol: str,
        handler: AsyncCallback,
    ) -> "DeltaWSClient":
        self._cbs.setdefault((channel, symbol), []).append(handler)
        return self

    def on_reconnect_full_resync(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Awaitable hook fired after every reconnect, before trading resumes
        (spec 1.8: full REST reconciliation gate)."""
        self._reconnect_hook = hook  # type: ignore[assignment]

    @property
    def safe_to_trade(self) -> bool:
        return not self.is_reconnecting

    # -------------------------------------------------------------- subscribe
    # Subscriptions are the same for public and private; private gating happens
    # at time-of-send (auth message needs to be sent first).
    def add_subscription(self, channel: str, symbols: Sequence[str] | None = None) -> None:
        self._subs.setdefault(channel, [])
        for s in symbols or [""]:
            if s not in self._subs[channel]:
                self._subs[channel].append(s)

    def _subscribe_frames(self) -> list[str]:
        frames: list[str] = []
        # TODO(VERIFY): confirm channel naming for candlesticks subscriptions:
        # some feeds require per-symbol channel like "candlesticks_1h".
        for channel, syms in self._subs.items():
            for s in syms:
                frames.append(
                    json.dumps(
                        {
                            "type": "subscribe",
                            "channel": channel,
                            **({"symbol": s} if s else {}),
                        },
                        separators=(",", ":"),
                    )
                )
        # Private: channels need no extra message once auth OK (spec 1.8)
        return frames

    def _auth_frame(self) -> str | None:
        if not (self.auth_needed and self.signer):
            return None
        # per docs' "Authenticating" section:
        ts = self.signer.timestamp()
        # TODO(VERIFY): exact signed payload for the socket auth
        # (`method + timestamp + path` with method "GET" and path "/live").
        sig = sign_request("GET", "/live", "", "", ts, self.signer.api_secret)
        return json.dumps(
            {
                "type": "auth",
                "api-key": self.signer.api_key,
                "signature": sig,
                "timestamp": ts,
            },
            separators=(",", ":"),
        )

    # ------------------------------------------------------------- lifecycle
    async def __aenter__(self) -> "DeltaWSClient":
        self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.stop()

    def start(self) -> None:
        self._closing = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # --------------------------------------------------------------- runner
    async def _run(self) -> None:
        attempt = 0
        while not self._closing:
            try:
                await self._connect_and_read()
                attempt = 0
            except asyncio.CancelledError:
                raise
            except _Reconnect:
                # stale heartbeat or abrupt close (transport code) → sleep loop
                pass
            except Exception:
                logger.exception("ws run loop crashed; reconnecting")
            delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * (2**attempt))
            delay += random.uniform(0, delay / 4)  # jitter (spec 1.8)
            attempt += 1
            self.is_reconnecting = True
            logger.info("ws reconnecting in %.1fs (attempt %s)", delay, attempt)
            await asyncio.sleep(min(delay, BACKOFF_MAX_S))

    async def _connect_and_read(self) -> None:
        # TODO(VERIFY): India prod socket = wss://socket.india.delta.exchange;
        # testnet socket URL must be confirmed in docs (config.py carries both).
        async with websockets.connect(self.url) as ws:
            self._ws = ws
            self.last_heartbeat = time.monotonic()
            auth_frame = self._auth_frame()
            if auth_frame:
                await ws.send(auth_frame)
                # gating: private subscribes wait for auth; simplest robust
                # policy is to send them after a short delay + poll for ack.
                # TODO(VERIFY): docs' exact auth ack frame.
            frames = self._subscribe_frames()
            if frames and not auth_frame:
                # public-only subscriptions can go straight away
                for f in frames:
                    if self._ws is None:
                        break
                    await ws.send(f)
            if self.is_reconnecting:
                # spec 1.8: after reconnect, resubscribe, then run full REST
                # reconciliation (strategy layer) before trading resumes.
                if self._reconnect_hook is not None:
                    await self._reconnect_hook()
                self.is_reconnecting = False
                if self.auth_needed:
                    # resubscribe private channels after auth ack (gated path)
                    pass
            reader = asyncio.create_task(self._read_loop(ws))
            try:
                while True:
                    await asyncio.sleep(1.0)
                    if time.monotonic() - self.last_heartbeat > HEARTBEAT_TIMEOUT_S:
                        logger.warning("ws heartbeat stale (%.0fs); forcing reconnect",
                                       time.monotonic() - self.last_heartbeat)
                        raise _Reconnect()
            finally:
                reader.cancel()
                self._ws = None

    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            await self._dispatch(msg)

    async def _dispatch(self, msg: Mapping[str, Any]) -> None:
        mtype = msg.get("type", "")
        if mtype == "heartbeat" or mtype == "channel_heartbeat":
            self.last_heartbeat = time.monotonic()
            return
        if mtype == "success" and ("authenticated" in str(msg.get("message", "")).lower() or
                                   "auth" in str(msg.get("message", "")).lower()):
            # private channels subscribed only after the auth ack (spec 1.8)
            # TODO(VERIFY): docs' exact auth ack frame.
            for f in self._subscribe_frames():
                if self._ws is not None:
                    await self._ws.send(f)
            return

        channel = str(msg.get("channel", ""))
        if not channel:
            return
        payload = msg.get("data", msg)  # candle frames use data/msg keys
        symbol = str(msg.get("symbol", ""))
        self.last_heartbeat = time.monotonic()  # any frame proves liveness
        for key in ((channel, symbol), (channel, ""), (channel, "*")):
            for cb in self._cbs.get(key, []):
                try:
                    await cb(channel, symbol, dict(payload))
                except Exception:
                    logger.exception("callback error on %s/%s", channel, symbol)

    # ------------------------------------------------- typed event decoders
    @staticmethod
    def parse_candle(channel: str, symbol: str, payload: Mapping[str, Any]) -> Candle | None:
        """Decode a candlesticks frame; `closed` True only for the final bar
        (strategies see closed candles only — spec 3.6 no-lookahead)."""
        ts = payload.get("time") or payload.get("timestamp")
        if ts is None:
            return None
        from datetime import datetime, timezone

        t = float(ts) / (1e6 if float(ts) > 1e12 else 1.0)
        return Candle(
            symbol=symbol,
            resolution=str(payload.get("resolution", "")),
            ts=datetime.fromtimestamp(t, tz=timezone.utc),
            open=float(payload.get("open", 0) or 0),
            high=float(payload.get("high", 0) or 0),
            low=float(payload.get("low", 0) or 0),
            close=float(payload.get("close", 0) or 0),
            volume=float(payload.get("volume", 0) or 0),
            closed=bool(payload.get("is_closed", False)),
        )

    @staticmethod
    def parse_ticker(symbol: str, payload: Mapping[str, Any]) -> Ticker:
        from decimal import Decimal

        quotes = payload.get("quotes") or {}
        return Ticker(
            symbol=symbol,
            mark_price=Decimal(str(payload["mark_price"])) if payload.get("mark_price") else None,
            spot_price=Decimal(str(payload["spot_price"])) if payload.get("spot_price") else None,
            best_bid=Decimal(str(quotes["best_bid"])) if quotes.get("best_bid") else None,
            best_ask=Decimal(str(quotes["best_ask"])) if quotes.get("best_ask") else None,
            volume=Decimal(str(payload.get("vol"))) if payload.get("vol") else None,
            oi=Decimal(str(payload.get("oi"))) if payload.get("oi") else None,
            strike_price=Decimal(str(payload["strike_price"])) if payload.get("strike_price") else None,
            expiry_date=payload.get("expiry_date"),
            raw=dict(payload),
        )

    @staticmethod
    def parse_fill(payload: Mapping[str, Any]) -> FillEvent:
        from decimal import Decimal

        return FillEvent(
            client_order_id=str(payload.get("client_order_id", "")),
            symbol=str(payload.get("product_symbol", "")),
            product_id=int(payload["product_id"]) if payload.get("product_id") else None,
            side=str(payload.get("side", "buy")),
            size=int(payload.get("size", 0) or 0),
            price=Decimal(str(payload.get("avg_fill_price") or payload.get("price", "0"))),
            fee=Decimal(str(payload.get("commission", "0"))),
            raw=dict(payload),
        )

    @staticmethod
    def parse_position(payload: Mapping[str, Any]) -> PositionSnapshot:
        from decimal import Decimal

        return PositionSnapshot(
            symbol=str(payload.get("product_symbol", payload.get("symbol", ""))),
            product_id=int(payload["product_id"]) if payload.get("product_id") else None,
            net_size=int(payload.get("size", 0) or 0),
            entry_price=(
                Decimal(str(payload["entry_price"])) if payload.get("entry_price") else None
            ),
            mark_price=(
                Decimal(str(payload["mark_price"])) if payload.get("mark_price") else None
            ),
            liquidation_price=(
                Decimal(str(payload["liquidation_price"])) if payload.get("liquidation_price") else None
            ),
            margin=Decimal(str(payload["margin"])) if payload.get("margin") else None,
            raw=dict(payload),
        )
