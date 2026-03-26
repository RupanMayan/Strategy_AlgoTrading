"""
util/notifier.py  —  Telegram alert delivery for Nifty Short Straddle
═══════════════════════════════════════════════════════════════════════════════════
Dual-path notification system:

  PRIMARY  : OpenAlgo Telegram API  — client.telegram(username, message)
  FALLBACK : Direct Telegram Bot API — POST /bot{token}/sendMessage
             Used ONLY when OpenAlgo itself is unreachable, to notify
             the operator that OpenAlgo is down.

Design:
  ┌─────────────────────┐     queue      ┌──────────────────────────────────┐
  │  notify("msg")      │ ─────────────► │  background daemon thread        │
  │  (returns instantly) │               │  _worker_loop()                  │
  └─────────────────────┘                │    → _send_via_openalgo()        │
                                         │    → if OpenAlgo fails:          │
                                         │        _send_via_bot_api()       │
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

import requests

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

# ── Telegram API base URL (direct Bot API fallback) ─────────────────────────
_TELEGRAM_API_BASE = "https://api.telegram.org"

# ── Message constraints ─────────────────────────────────────────────────────
_MAX_MSG_LEN   = 4096        # Telegram hard limit (characters per sendMessage)
_TRUNCATE_TAIL = "[…truncated]"

# ── Retry policy ────────────────────────────────────────────────────────────
_MAX_ATTEMPTS    = 3
_BACKOFF_DELAYS  = (1, 3)    # seconds before attempt 2, attempt 3

# ── HTTP timeouts (seconds) — for direct Bot API fallback ───────────────────
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT    = 8

# ── Queue sentinel value ────────────────────────────────────────────────────
_SENTINEL = None


# ═══════════════════════════════════════════════════════════════════════════════
#  TelegramNotifier — encapsulates queue, worker thread, and delivery logic
# ═══════════════════════════════════════════════════════════════════════════════

class TelegramNotifier:
    """
    Asynchronous Telegram message delivery via a background daemon thread.

    Primary path  : OpenAlgo SDK client.telegram()
    Fallback path : Direct Telegram Bot API (only when OpenAlgo is unreachable)
    """

    def __init__(self) -> None:
        self._send_queue: queue.Queue[str | None] = queue.Queue()
        self._http_session: requests.Session | None = None
        self._session_lock = threading.Lock()
        self._worker_thread: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._openalgo_client = None
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
        """Return a cached OpenAlgo client for Telegram API calls."""
        if self._openalgo_client is None:
            with self._openalgo_client_lock:
                if self._openalgo_client is None:
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
                        return None
        return self._openalgo_client

    # ── HTTP session (for direct Bot API fallback) ──────────────────────────

    def _get_session(self) -> requests.Session:
        """Return the shared HTTP session for direct Bot API calls."""
        with self._session_lock:
            if self._http_session is None:
                self._http_session = requests.Session()
                self._http_session.headers.update({
                    "Content-Type": "application/json",
                    "User-Agent": f"NiftyShortStraddle/{_get_version()}",
                })
            return self._http_session

    # ── Enabled check ───────────────────────────────────────────────────────

    def _is_enabled(self) -> bool:
        """Return True if Telegram notifications are enabled."""
        cfg = self._get_config()
        if cfg is None:
            return False
        return bool(cfg.TELEGRAM_ENABLED and cfg.TELEGRAM_USERNAME)

    def _has_bot_api_fallback(self) -> bool:
        """Return True if direct Bot API credentials are configured."""
        cfg = self._get_config()
        if cfg is None:
            return False
        return bool(cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID)

    # ── Message formatting ──────────────────────────────────────────────────

    def _build_text(self, msg: str) -> str:
        """Prepend the strategy name + version header and enforce 4096-char limit."""
        cfg = self._get_config()
        strategy_name = cfg.STRATEGY_NAME if cfg else "Short Straddle"
        header = f"[{strategy_name} v{_get_version()}]\n"

        available = _MAX_MSG_LEN - len(header) - len(_TRUNCATE_TAIL)
        if len(msg) > available:
            warn(
                f"Telegram message truncated from {len(msg)} to {available} chars "
                f"(Telegram limit: {_MAX_MSG_LEN})"
            )
            msg = msg[:available] + _TRUNCATE_TAIL

        return header + msg

    # ── PRIMARY: OpenAlgo Telegram API ──────────────────────────────────────

    def _send_via_openalgo(self, text: str) -> bool:
        """
        Send via OpenAlgo's Telegram API.

        Returns True on success, False on failure (caller should try fallback).
        """
        cfg = self._get_config()
        if cfg is None or not cfg.TELEGRAM_USERNAME:
            return False

        client = self._get_openalgo_client()
        if client is None:
            return False

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

    # ── FALLBACK: Direct Telegram Bot API ───────────────────────────────────

    def _send_via_bot_api(self, text: str) -> bool:
        """
        Send via direct Telegram Bot API (fallback).

        Used ONLY when OpenAlgo is unreachable, to notify the operator.
        Returns True on success or permanent failure (stop retrying).
        Returns False on transient failure (caller should retry).
        """
        cfg = self._get_config()
        if cfg is None or not cfg.TELEGRAM_BOT_TOKEN or not cfg.TELEGRAM_CHAT_ID:
            return True  # No fallback credentials — skip silently

        url = f"{_TELEGRAM_API_BASE}/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": cfg.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        }

        try:
            resp = self._get_session().post(
                url,
                json=data,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )

            if resp.status_code == 200:
                debug("Telegram delivered via direct Bot API (fallback)")
                return True

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", _BACKOFF_DELAYS[0]))
                warn(f"Telegram Bot API 429 rate-limited — waiting {retry_after}s")
                time.sleep(retry_after)
                return False

            if 500 <= resp.status_code < 600:
                warn(f"Telegram Bot API server error {resp.status_code} — will retry")
                return False

            error(f"Telegram Bot API permanent error {resp.status_code}: {resp.text[:200]}")
            return True  # Stop retrying

        except requests.exceptions.Timeout:
            warn("Telegram Bot API timed out — will retry")
            return False

        except requests.exceptions.ConnectionError as exc:
            warn(f"Telegram Bot API connection error: {exc} — will retry")
            return False

        except Exception as exc:
            warn(f"Telegram Bot API unexpected error: {exc}")
            return True  # Unknown — don't loop forever

    # ── Send with retry (dual-path) ─────────────────────────────────────────

    def _send_with_retry(self, text: str) -> None:
        """
        Attempt delivery: OpenAlgo first, direct Bot API fallback on failure.

        The fallback is retried up to _MAX_ATTEMPTS times with exponential backoff.
        """
        if not self._is_enabled():
            debug("Telegram disabled — skipping")
            return

        # Primary: try OpenAlgo API (single attempt — it's fast or down)
        if self._send_via_openalgo(text):
            return

        # OpenAlgo failed — try direct Bot API as fallback
        if not self._has_bot_api_fallback():
            warn("OpenAlgo Telegram API failed and no Bot API fallback configured — message dropped")
            return

        warn("OpenAlgo Telegram API unreachable — falling back to direct Bot API")
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            success = self._send_via_bot_api(text)
            if success:
                return

            if attempt < _MAX_ATTEMPTS:
                delay = _BACKOFF_DELAYS[min(attempt - 1, len(_BACKOFF_DELAYS) - 1)]
                debug(f"Bot API fallback attempt {attempt} failed — retrying in {delay}s")
                time.sleep(delay)

        warn(
            f"Telegram delivery failed after OpenAlgo + {_MAX_ATTEMPTS} Bot API attempts — "
            "message dropped"
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
        Block until all queued messages have been delivered (or time out).

        Returns True if all delivered, False if timeout expired.
        """
        if self._send_queue.empty():
            return True

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._send_queue.empty():
                return True
            time.sleep(0.1)
        return False


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
#    OPENALGO_APIKEY=x OPENALGO_USERNAME=your_user TELEGRAM_BOT_TOKEN=<token> \
#    TELEGRAM_CHAT_ID=<id> python -m util.notifier
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
        print(f"  Bot token  : {'***' + cfg.TELEGRAM_BOT_TOKEN[-6:] if cfg.TELEGRAM_BOT_TOKEN else '(not set — no fallback)'}")
        print(f"  Chat ID    : {cfg.TELEGRAM_CHAT_ID or '(not set — no fallback)'}")
        print(f"  Strategy   : {cfg.STRATEGY_NAME}  v{_get_version()}")
        print()
        print("  Sending test messages — check your Telegram chat...")
        print()

        notify("🧪 <b>Notifier self-test</b>\nDelivery: OpenAlgo API (primary) → Bot API (fallback)")
        notify(f"html_escape test: VIX {html_escape('14.5 < 18.0 & > 12.0')}")
        notify("This is the final test message. If you see all 3, delivery is working ✓")

        print("  Messages enqueued. Flushing (waiting up to 15s for delivery)...")
        delivered = flush(timeout=15)

        print()
        if delivered:
            print("  Flush completed — all messages delivered ✓")
        else:
            print("  Flush TIMED OUT — check network / credentials")
            sys.exit(1)

    print("─" * 72)
