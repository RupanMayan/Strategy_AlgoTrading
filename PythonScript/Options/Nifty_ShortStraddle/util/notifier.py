"""
util/notifier.py  —  Telegram alert delivery for Nifty Short Straddle
═══════════════════════════════════════════════════════════════════════════════════
Sends Telegram notifications via the OpenAlgo Telegram API.

  OpenAlgo API  — client.telegram(username, message)

Design:
  ┌─────────────────────┐     queue      ┌──────────────────────────────────┐
  │  notify("msg")      │ ─────────────► │  background daemon thread        │
  │  (returns instantly) │               │  _worker_loop()                  │
  └─────────────────────┘                │    → _send_with_retry()          │
                                         │    → OpenAlgo client.telegram()  │
                                         └──────────────────────────────────┘

Message limits:
  Telegram hard limit: 4096 characters per message.
  Messages exceeding this are truncated with a "[…truncated]" suffix.

Usage:
    from util.notifier import notify
    notify("Entry placed: CE NIFTY25MAR2623000CE @ Rs.123.50")

    from util.notifier import flush
    flush(timeout=10)
═══════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import queue
import threading
import time

from util.logger import debug, error, info, warn

__all__ = ["TelegramNotifier", "notify", "telegram", "html_escape", "flush"]

# ── Strategy version — resolved lazily from src._shared.VERSION to avoid
#    circular imports (src._shared imports util.notifier at module level).
def _get_version() -> str:
    try:
        from src._shared import VERSION
        return VERSION
    except ImportError:
        return "7.2.0"

# ── Message constraints ─────────────────────────────────────────────────────
_MAX_MSG_LEN   = 4096        # Telegram hard limit (characters per sendMessage)
_TRUNCATE_TAIL = "[…truncated]"

# ── Retry policy ────────────────────────────────────────────────────────────
_MAX_ATTEMPTS    = 3
_BACKOFF_DELAYS  = (1, 3)    # seconds before attempt 2, attempt 3

# ── Queue sentinel value ────────────────────────────────────────────────────
_SENTINEL = None


# ═══════════════════════════════════════════════════════════════════════════════
#  TelegramNotifier — encapsulates queue, worker thread, and delivery logic
# ═══════════════════════════════════════════════════════════════════════════════

class TelegramNotifier:
    """
    Asynchronous Telegram message delivery via a background daemon thread.

    Delivery channel: OpenAlgo SDK client.telegram(username, message)
    """

    _CLIENT_FAILED = object()  # sentinel: client construction was attempted and failed

    def __init__(self) -> None:
        self._send_queue: queue.Queue[str | None] = queue.Queue()
        self._worker_thread: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._openalgo_client: object | None = None  # None=not tried, _CLIENT_FAILED=failed
        self._openalgo_client_lock = threading.Lock()

    # ── Config access ───────────────────────────────────────────────────────

    @staticmethod
    def _get_config():
        """Lazily fetch the Config singleton. Returns None if not yet loaded."""
        try:
            from util.config_util import cfg
            return cfg
        except Exception:
            return None

    # ── OpenAlgo client (lazy singleton) ────────────────────────────────────

    def _get_openalgo_client(self):
        """Return a cached OpenAlgo client for Telegram API calls.

        Uses a sentinel to avoid retrying construction on every message
        when it has already failed once. Double-checked locking covers
        both the None and _CLIENT_FAILED states.
        """
        if self._openalgo_client is self._CLIENT_FAILED:
            return None
        if self._openalgo_client is not None:
            return self._openalgo_client
        with self._openalgo_client_lock:
            # Re-check both sentinels under the lock
            if self._openalgo_client is self._CLIENT_FAILED:
                return None
            if self._openalgo_client is not None:
                return self._openalgo_client
            cfg = self._get_config()
            if cfg is None:
                return None
            try:
                from openalgo import api as OpenAlgoClient
                self._openalgo_client = OpenAlgoClient(
                    api_key=cfg.OPENALGO_API_KEY,
                    host=cfg.OPENALGO_HOST,
                )
            except Exception as exc:
                warn(f"Failed to create OpenAlgo client for Telegram: {exc}")
                self._openalgo_client = self._CLIENT_FAILED
                return None
        return self._openalgo_client

    # ── Enabled check ───────────────────────────────────────────────────────

    def _is_enabled(self) -> bool:
        """Return True if Telegram notifications are enabled and username is set."""
        cfg = self._get_config()
        if cfg is None:
            return False
        return bool(cfg.TELEGRAM_ENABLED and cfg.TELEGRAM_USERNAME)

    # ── Message formatting ──────────────────────────────────────────────────

    def _build_text(self, msg: str) -> str:
        """Prepend the strategy name + version header and enforce 4096-char limit."""
        cfg = self._get_config()
        strategy_name = cfg.STRATEGY_NAME if cfg else "Short Straddle"
        header = f"[{strategy_name} v{_get_version()}]\n"

        available = max(0, _MAX_MSG_LEN - len(header) - len(_TRUNCATE_TAIL))
        if len(msg) > available:
            warn(
                f"Telegram message truncated from {len(msg)} to {available} chars "
                f"(Telegram limit: {_MAX_MSG_LEN})"
            )
            msg = msg[:available] + _TRUNCATE_TAIL

        return header + msg

    # ── Send via OpenAlgo Telegram API ──────────────────────────────────────

    def _send_once(self, text: str) -> bool:
        """
        Attempt a single send via OpenAlgo's Telegram API.

        Returns True on success or permanent failure (stop retrying).
        Returns False on transient failure (caller should retry).
        """
        cfg = self._get_config()
        if cfg is None or not cfg.TELEGRAM_USERNAME:
            return True  # Config gone — skip silently

        client = self._get_openalgo_client()
        if client is None:
            return True  # Client construction failed permanently — skip

        try:
            resp = client.telegram(
                username=cfg.TELEGRAM_USERNAME,
                message=text,
            )
            if isinstance(resp, dict) and resp.get("status") == "success":
                debug("Telegram delivered via OpenAlgo API")
                return True
            warn(f"OpenAlgo Telegram API non-success response: {resp}")
            return False
        except Exception as exc:
            warn(f"OpenAlgo Telegram API failed: {exc}")
            return False

    # ── Send with retry ───────────────────────────────────────────────────

    def _send_with_retry(self, text: str) -> None:
        """Attempt delivery up to _MAX_ATTEMPTS times with exponential backoff."""
        if not self._is_enabled():
            debug("Telegram disabled — skipping")
            return

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            if self._send_once(text):
                return

            if attempt < _MAX_ATTEMPTS:
                delay = _BACKOFF_DELAYS[min(attempt - 1, len(_BACKOFF_DELAYS) - 1)]
                debug(f"Telegram attempt {attempt} failed — retrying in {delay}s")
                time.sleep(delay)

        warn(
            f"Telegram delivery failed after {_MAX_ATTEMPTS} attempts — "
            "message dropped. Check OpenAlgo connection."
        )

    # ── Worker thread ───────────────────────────────────────────────────────

    def _worker_loop(self) -> None:
        """Background daemon thread: drain the send queue and deliver each message."""
        while True:
            try:
                text = self._send_queue.get()

                if text is _SENTINEL:
                    self._send_queue.task_done()
                    return

                self._send_with_retry(text)
                self._send_queue.task_done()

            except Exception as exc:
                warn(f"Telegram worker unexpected error: {exc}")
                try:
                    self._send_queue.task_done()
                except ValueError:
                    pass

    def _ensure_worker_running(self) -> None:
        """Start the background worker thread if it is not already alive."""
        with self._worker_lock:
            if self._worker_thread is None or not self._worker_thread.is_alive():
                self._worker_thread = threading.Thread(
                    target=self._worker_loop,
                    name="telegram-sender",
                    daemon=True,
                )
                self._worker_thread.start()
                debug("Telegram worker thread started")

    # ═══════════════════════════════════════════════════════════════════════════
    #  Public API
    # ═══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def html_escape(text: str) -> str:
        """Escape characters that have special meaning in Telegram HTML mode."""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def notify(self, msg: str) -> None:
        """
        Enqueue a Telegram message for background delivery.

        Returns immediately — delivery is handled by a background daemon thread.
        Never raises. Silently skips if Telegram is disabled.
        """
        if not self._is_enabled():
            return

        try:
            text = self._build_text(msg)
            self._ensure_worker_running()
            self._send_queue.put(text)
        except Exception as exc:
            warn(f"Telegram enqueue failed: {exc}")

    def flush(self, timeout: float = 10.0) -> bool:
        """
        Block until all queued messages have been fully delivered (or time out).

        Uses queue.join() via a helper thread to correctly wait for task_done()
        on every in-flight message, not just queue emptiness.

        Returns True if all delivered, False if timeout expired.
        """
        if self._send_queue.empty():
            return True

        joiner = threading.Thread(target=self._send_queue.join, daemon=True)
        joiner.start()
        joiner.join(timeout=timeout)
        return not joiner.is_alive()


# ═══════════════════════════════════════════════════════════════════════════════
#  Module-level singleton + backward-compatible function API
# ═══════════════════════════════════════════════════════════════════════════════

_notifier_instance = TelegramNotifier()


def html_escape(text: str) -> str:
    """Backward-compatible wrapper — delegates to TelegramNotifier.html_escape()."""
    return TelegramNotifier.html_escape(text)


def notify(msg: str) -> None:
    """Backward-compatible wrapper — delegates to the singleton."""
    _notifier_instance.notify(msg)


# ── Drop-in alias — matches the original function name ───────────────────────
telegram = notify


def flush(timeout: float = 10.0) -> bool:
    """Backward-compatible wrapper — delegates to the singleton."""
    return _notifier_instance.flush(timeout)


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI self-test
#
#  Usage (from the strategy directory, using the algo_trading venv):
#
#    OPENALGO_APIKEY=x OPENALGO_USERNAME=your_user python -m util.notifier
#
#    # Dry-run (disabled mode — verifies skip logic):
#    python -m util.notifier --dry-run
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys

    dry_run = "--dry-run" in sys.argv

    print("─" * 72)
    print("  NOTIFIER MODULE SELF-TEST")
    print("─" * 72)

    from util.config_util import cfg

    if dry_run or cfg is None or not cfg.TELEGRAM_ENABLED:
        print("  Mode: DRY-RUN (Telegram disabled or --dry-run flag)")
        print()
        print("  Testing html_escape():")
        cases = [
            ("plain text",      "plain text"),
            ("a < b & c > d",   "a &lt; b &amp; c &gt; d"),
            ("already &amp;",   "already &amp;amp;"),
            ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"),
        ]
        ok = True
        for raw, expected in cases:
            got    = html_escape(raw)
            match  = got == expected
            ok     = ok and match
            status = "✓" if match else "✗"
            print(f"  {status}  input={raw!r:40}  got={got!r}")

        print()
        print("  Testing _build_text() truncation:")
        long_msg = "x" * 5000
        built    = _notifier_instance._build_text(long_msg)
        trunc_ok = len(built) <= _MAX_MSG_LEN and built.endswith(_TRUNCATE_TAIL)
        ok       = ok and trunc_ok
        print(f"  {'✓' if trunc_ok else '✗'}  5000-char message truncated to "
              f"{len(built)} chars (limit: {_MAX_MSG_LEN}) ending with {_TRUNCATE_TAIL!r}")

        print()
        print("  Testing notify() with Telegram disabled (should silently skip):")
        notify("This message should be silently dropped (Telegram disabled)")
        time.sleep(0.2)
        queue_empty = _notifier_instance._send_queue.empty()
        print(f"  {'✓' if queue_empty else '✗'}  Queue empty after notify() with Telegram disabled")

        print()
        if ok and queue_empty:
            print("  All dry-run assertions passed ✓")
        else:
            print("  SOME ASSERTIONS FAILED ✗")
            sys.exit(1)

    else:
        print(f"  Username   : {cfg.TELEGRAM_USERNAME}")
        print(f"  Strategy   : {cfg.STRATEGY_NAME}  v{_get_version()}")
        print()
        print("  Sending test messages via OpenAlgo — check your Telegram chat...")
        print()

        notify("🧪 <b>Notifier self-test</b>\nDelivery via OpenAlgo Telegram API")
        notify(f"html_escape test: VIX {html_escape('14.5 < 18.0 & > 12.0')}")
        notify("This is the final test message. If you see all 3, delivery is working ✓")

        print("  Messages enqueued. Flushing (waiting up to 15s for delivery)...")
        delivered = flush(timeout=15)

        print()
        if delivered:
            print("  Flush completed — all messages delivered ✓")
        else:
            print("  Flush TIMED OUT — check OpenAlgo connection / credentials")
            sys.exit(1)

    print("─" * 72)
