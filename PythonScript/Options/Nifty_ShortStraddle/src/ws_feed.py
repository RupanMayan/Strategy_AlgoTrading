"""
src/ws_feed.py  —  WebSocket Live Feed Manager
═══════════════════════════════════════════════════════════════════════
Production-grade WebSocket client for real-time LTP streaming from
OpenAlgo's WebSocket API.

Replaces the 15-second REST polling loop with sub-second price updates
for faster SL execution.

Features:
  • Thread-safe daemon thread — runs alongside APScheduler
  • Auto-reconnect with exponential backoff (1s → 30s cap)
  • Ping/pong heartbeat handling (server pings every 30s)
  • Graceful shutdown: unsubscribe all → close connection
  • Telegram alerts on persistent connection failures
  • LTP cache integration via _shared.update_ltp_cache()

Lifecycle:
  1. StrategyCore.run() calls ws_feed.start() at startup
  2. After entry, subscribe CE/PE option symbols
  3. On position close, unsubscribe option symbols
  4. On shutdown, ws_feed.stop() cleanly disconnects

Usage:
  feed = WebSocketFeed()
  feed.start()                                    # connect + auth
  feed.subscribe("NIFTY25MAR2623000CE", "NFO")    # add symbol
  feed.unsubscribe("NIFTY25MAR2623000CE", "NFO")  # remove symbol
  feed.stop()                                     # graceful shutdown
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime
from typing import Any

from src._shared import (
    cfg,
    info, warn, error, debug,
    telegram,
    now_ist,
    update_ltp_cache,
    INDEX_EXCH, OPTION_EXCH,
    VIX_SYMBOL,
)


class WebSocketFeed:
    """
    Thread-safe WebSocket client for real-time LTP streaming.

    Runs an asyncio event loop in a daemon thread. All public methods
    are thread-safe and can be called from the main (APScheduler) thread.
    """

    # Reconnect backoff parameters
    _RECONNECT_BASE_DELAY: float = 1.0
    _RECONNECT_MAX_DELAY: float = 30.0
    _RECONNECT_BACKOFF_FACTOR: float = 2.0

    # Alert after this many consecutive reconnect failures
    _ALERT_AFTER_FAILURES: int = 3

    def __init__(self) -> None:
        self._ws: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Track active subscriptions: {(symbol, exchange, mode)}
        self._subscriptions: set[tuple[str, str, int]] = set()
        self._sub_lock = threading.Lock()

        # Reconnect state
        self._consecutive_failures: int = 0
        self._alert_sent: bool = False

        # Connection state
        self._authenticated: bool = False
        self._connected: bool = False

    # ── Public API (called from main thread) ─────────────────────────────

    def start(self) -> None:
        """Start the WebSocket feed in a daemon thread."""
        if self._thread is not None and self._thread.is_alive():
            warn("WebSocketFeed.start() called but already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="ws-feed",
            daemon=True,
        )
        self._thread.start()
        info("WebSocket feed: daemon thread started")

    def stop(self) -> None:
        """Gracefully stop the WebSocket feed."""
        if self._thread is None or not self._thread.is_alive():
            return

        info("WebSocket feed: shutting down...")
        self._stop_event.set()

        # Schedule graceful close and await completion before joining
        if self._loop is not None and self._loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._graceful_close(), self._loop)
            try:
                fut.result(timeout=5)
            except Exception:
                pass  # Best-effort — thread join will follow

        self._thread.join(timeout=10)
        if self._thread.is_alive():
            warn("WebSocket feed: thread did not stop within 10s — abandoning")
        else:
            info("WebSocket feed: stopped cleanly")
        self._thread = None

    @property
    def is_connected(self) -> bool:
        """True if WebSocket is connected and authenticated."""
        return self._connected and self._authenticated

    def subscribe(self, symbol: str, exchange: str, mode: int = 1) -> None:
        """
        Subscribe to a symbol for live LTP updates.

        Parameters
        ----------
        symbol   : e.g. "NIFTY25MAR2623000CE" or "NIFTY" or "INDIAVIX"
        exchange : e.g. "NFO", "NSE_INDEX"
        mode     : 1=LTP, 2=Quote, 3=Depth (default: 1 for LTP only)
        """
        key = (symbol, exchange, mode)
        with self._sub_lock:
            if key in self._subscriptions:
                return
            self._subscriptions.add(key)

        debug(f"WebSocket: queuing subscribe {symbol}@{exchange} mode={mode}")

        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._send_subscribe(symbol, exchange, mode), self._loop
            )

    def unsubscribe(self, symbol: str, exchange: str, mode: int = 1) -> None:
        """Unsubscribe from a symbol."""
        key = (symbol, exchange, mode)
        with self._sub_lock:
            self._subscriptions.discard(key)

        debug(f"WebSocket: queuing unsubscribe {symbol}@{exchange} mode={mode}")

        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._send_unsubscribe(symbol, exchange, mode), self._loop
            )

    def subscribe_position_symbols(self, symbol_ce: str, symbol_pe: str) -> None:
        """Subscribe to both option leg symbols after entry."""
        if symbol_ce:
            self.subscribe(symbol_ce, OPTION_EXCH)
        if symbol_pe:
            self.subscribe(symbol_pe, OPTION_EXCH)
        info(f"WebSocket: subscribed CE={symbol_ce}  PE={symbol_pe}")

    def unsubscribe_position_symbols(self, symbol_ce: str, symbol_pe: str) -> None:
        """Unsubscribe option leg symbols after position close."""
        if symbol_ce:
            self.unsubscribe(symbol_ce, OPTION_EXCH)
        if symbol_pe:
            self.unsubscribe(symbol_pe, OPTION_EXCH)
        info(f"WebSocket: unsubscribed CE={symbol_ce}  PE={symbol_pe}")

    def subscribe_market_symbols(self) -> None:
        """Subscribe to underlying spot and VIX for market monitoring."""
        self.subscribe(cfg.UNDERLYING, INDEX_EXCH)
        self.subscribe(VIX_SYMBOL, INDEX_EXCH)
        info(f"WebSocket: subscribed market symbols ({cfg.UNDERLYING}, {VIX_SYMBOL})")

    # ── Internal: event loop runner ──────────────────────────────────────

    def _run_loop(self) -> None:
        """Entry point for the daemon thread — runs the async event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connection_loop())
        except Exception as exc:
            error(f"WebSocket feed: event loop crashed: {exc}")
        finally:
            self._loop.close()
            self._loop = None
            self._connected = False
            self._authenticated = False

    async def _connection_loop(self) -> None:
        """
        Outer loop: connect → authenticate → receive messages.
        On disconnect, reconnect with exponential backoff.
        """
        import websockets

        delay = self._RECONNECT_BASE_DELAY
        max_delay = getattr(cfg, "WEBSOCKET_RECONNECT_MAX_S", self._RECONNECT_MAX_DELAY)

        while not self._stop_event.is_set():
            ws_url = self._build_ws_url()
            try:
                debug(f"WebSocket: connecting to {ws_url}")
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True

                    # Authenticate
                    if not await self._authenticate():
                        warn("WebSocket: authentication failed — will retry")
                        self._connected = False
                        self._ws = None
                        await self._backoff_delay(delay)
                        delay = min(delay * self._RECONNECT_BACKOFF_FACTOR, max_delay)
                        continue

                    self._authenticated = True
                    self._consecutive_failures = 0
                    self._alert_sent = False
                    delay = self._RECONNECT_BASE_DELAY
                    info("WebSocket: connected and authenticated")

                    # Re-subscribe all active symbols
                    await self._resubscribe_all()

                    # Receive loop
                    await self._receive_loop(ws)

            except asyncio.CancelledError:
                break

            except Exception as exc:
                self._connected = False
                self._authenticated = False
                self._ws = None
                self._consecutive_failures += 1

                warn(
                    f"WebSocket: connection lost ({exc}) — "
                    f"reconnecting in {delay:.1f}s "
                    f"(failure #{self._consecutive_failures})"
                )

                # Alert via Telegram after persistent failures
                if (
                    self._consecutive_failures >= self._ALERT_AFTER_FAILURES
                    and not self._alert_sent
                ):
                    telegram(
                        f"⚠️ WebSocket feed DOWN\n"
                        f"Failed {self._consecutive_failures} consecutive reconnects\n"
                        f"Last error: {exc}\n"
                        f"Falling back to REST polling for LTP"
                    )
                    self._alert_sent = True

                await self._backoff_delay(delay)
                delay = min(delay * self._RECONNECT_BACKOFF_FACTOR, max_delay)

        self._connected = False
        self._authenticated = False

    async def _receive_loop(self, ws: Any) -> None:
        """Process incoming WebSocket messages until disconnect or stop."""
        import websockets

        try:
            async for raw_msg in ws:
                if self._stop_event.is_set():
                    break

                try:
                    msg = json.loads(raw_msg) if isinstance(raw_msg, str) else raw_msg
                except (json.JSONDecodeError, TypeError):
                    debug(f"WebSocket: non-JSON message: {raw_msg!r:.200}")
                    continue

                self._handle_message(msg)

        except websockets.exceptions.ConnectionClosed as exc:
            info(f"WebSocket: connection closed (code={exc.code}, reason={exc.reason})")

    # ── Message handlers ─────────────────────────────────────────────────

    def _handle_message(self, msg: dict) -> None:
        """Route incoming messages by type."""
        msg_type = msg.get("type", "")

        if msg_type == "market_data":
            self._handle_market_data(msg)
        elif msg_type == "error":
            warn(
                f"WebSocket error: [{msg.get('code', 'UNKNOWN')}] "
                f"{msg.get('message', str(msg))}"
            )
        elif msg_type == "auth":
            debug(f"WebSocket auth response: {msg}")
        else:
            debug(f"WebSocket: unhandled message type '{msg_type}': {msg}")

    def _handle_market_data(self, msg: dict) -> None:
        """
        Process market_data messages and update the shared LTP cache.

        Expected format:
          {
            "type": "market_data",
            "mode": 1,
            "topic": "NIFTY25MAR2623000CE.NFO",
            "data": {"ltp": 150.25, ...}
          }
        """
        data = msg.get("data", {})
        topic = msg.get("topic", "")

        ltp = data.get("ltp")
        if ltp is None or not topic:
            return

        try:
            ltp_float = float(ltp)
        except (ValueError, TypeError):
            return

        if ltp_float <= 0:
            return

        # Parse topic: "SYMBOL.EXCHANGE"
        parts = topic.rsplit(".", 1)
        if len(parts) != 2:
            debug(f"WebSocket: unexpected topic format: {topic}")
            return

        symbol, exchange = parts
        update_ltp_cache(symbol, exchange, ltp_float)

    # ── Authentication ───────────────────────────────────────────────────

    async def _authenticate(self) -> bool:
        """Send authentication message and wait for response."""
        if self._ws is None:
            return False

        auth_msg = json.dumps({
            "action": "authenticate",
            "api_key": cfg.OPENALGO_API_KEY,
        })

        try:
            await self._ws.send(auth_msg)

            # Wait for auth response (timeout 10s)
            try:
                resp_raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
                resp = json.loads(resp_raw) if isinstance(resp_raw, str) else resp_raw
                status = resp.get("status", "")
                if status in ("success", "ok", "authenticated"):
                    debug(f"WebSocket: auth success: {resp}")
                    return True
                warn(f"WebSocket auth failed (status='{status}'): {resp}")
                return False
            except asyncio.TimeoutError:
                warn("WebSocket: auth response timeout (10s)")
                return False

        except Exception as exc:
            warn(f"WebSocket: auth send failed: {exc}")
            return False

    # ── Subscribe / Unsubscribe ──────────────────────────────────────────

    async def _send_subscribe(self, symbol: str, exchange: str, mode: int) -> None:
        """Send a subscribe message to the WebSocket server."""
        if self._ws is None or not self._authenticated:
            return
        msg = json.dumps({
            "action": "subscribe",
            "symbol": symbol,
            "exchange": exchange,
            "mode": mode,
        })
        try:
            await self._ws.send(msg)
            debug(f"WebSocket: sent subscribe {symbol}@{exchange} mode={mode}")
        except Exception as exc:
            warn(f"WebSocket: subscribe send failed ({symbol}): {exc}")

    async def _send_unsubscribe(self, symbol: str, exchange: str, mode: int) -> None:
        """Send an unsubscribe message to the WebSocket server."""
        if self._ws is None or not self._authenticated:
            return
        msg = json.dumps({
            "action": "unsubscribe",
            "symbol": symbol,
            "exchange": exchange,
            "mode": mode,
        })
        try:
            await self._ws.send(msg)
            debug(f"WebSocket: sent unsubscribe {symbol}@{exchange} mode={mode}")
        except Exception as exc:
            warn(f"WebSocket: unsubscribe send failed ({symbol}): {exc}")

    async def _resubscribe_all(self) -> None:
        """Re-subscribe all active symbols after reconnect."""
        with self._sub_lock:
            subs = list(self._subscriptions)

        if not subs:
            return

        info(f"WebSocket: re-subscribing {len(subs)} symbol(s) after reconnect")
        for symbol, exchange, mode in subs:
            await self._send_subscribe(symbol, exchange, mode)

    # ── Graceful shutdown ────────────────────────────────────────────────

    async def _graceful_close(self) -> None:
        """Unsubscribe all symbols and close the WebSocket connection."""
        if self._ws is None:
            return

        # Unsubscribe all
        with self._sub_lock:
            subs = list(self._subscriptions)

        for symbol, exchange, mode in subs:
            try:
                await self._send_unsubscribe(symbol, exchange, mode)
            except Exception:
                pass  # Best-effort on shutdown

        # Close connection
        try:
            await self._ws.close()
        except Exception:
            pass

        self._connected = False
        self._authenticated = False
        self._ws = None
        info("WebSocket: graceful close completed")

    # ── Helpers ───────────────────────────────────────────────────────────

    def _build_ws_url(self) -> str:
        """
        Derive WebSocket URL from cfg.OPENALGO_HOST.

        http://127.0.0.1:5000  →  ws://127.0.0.1:5000/ws
        https://myalgo.com     →  wss://myalgo.com/ws
        """
        host = cfg.OPENALGO_HOST.rstrip("/")
        if host.startswith("https://"):
            return host.replace("https://", "wss://", 1) + "/ws"
        if host.startswith("http://"):
            return host.replace("http://", "ws://", 1) + "/ws"
        return f"ws://{host}/ws"

    async def _backoff_delay(self, delay: float) -> None:
        """Sleep with stop_event awareness (check every 0.5s)."""
        elapsed = 0.0
        while elapsed < delay and not self._stop_event.is_set():
            await asyncio.sleep(min(0.5, delay - elapsed))
            elapsed += 0.5
