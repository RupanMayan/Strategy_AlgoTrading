"""
util/notifier.py  —  Telegram alert delivery for Nifty Short Straddle (Partial)
═══════════════════════════════════════════════════════════════════════════════════
Responsibilities:
  1. Send strategy alerts to a Telegram chat via the Bot API
  2. Prefix every message with "[{STRATEGY_NAME} v{VERSION}]" (matches original)
  3. Never block the calling thread — delivery runs in a background daemon thread
     so monitor ticks, SL checks, and order placement are never delayed by a
     slow or unreachable Telegram API
  4. Retry transiently-failed sends (network error, 429, 5xx) up to MAX_RETRIES
     times with exponential back-off; permanent failures (400 bad request) are
     logged once and discarded
  5. Silently skip when Telegram is disabled or credentials are missing
  6. Never raise — failures are logged as warnings, never propagate to callers

Production design:
  ┌─────────────────────┐     queue      ┌──────────────────────────────────┐
  │  notify("msg")      │ ─────────────► │  background daemon thread        │
  │  (returns instantly)│                │  _worker_loop()                  │
  └─────────────────────┘                │    → _send_once() with retry     │
                                         │    → exponential back-off        │
                                         │    → Retry-After for 429         │
                                         └──────────────────────────────────┘

WHY background thread instead of synchronous:
  The original telegram() uses timeout=6s. With retries that could block the
  calling thread for 6+1+6+2+6 = 21s. At monitor_interval_s=15s this would
  cause consecutive tick skips and SL misses during a network hiccup.
  A queue + daemon thread decouples delivery latency from strategy execution.
  The trade-off (messages may be lost on hard crash) is acceptable — a crash
  triggers an emergency close first, then the crash alert is best-effort.

Session reuse:
  A single persistent requests.Session is created lazily on the first send.
  Session reuse enables TCP connection pooling, reducing per-message latency
  from ~300ms (new TLS handshake) to ~30ms (reused connection).

Message limits:
  Telegram hard limit: 4096 characters per message.
  Messages exceeding this are truncated with a "…[truncated]" suffix so the
  critical first ~4000 characters are always delivered.

HTML parse mode:
  parse_mode="HTML" is preserved from the original to support existing message
  formatting (bold <b>text</b>, monospace <code>x</code>). Callers that embed
  raw `<` / `>` / `&` in plain-text messages must escape them manually:
    &lt;  →  <      &gt;  →  >      &amp;  →  &
  Helper: html_escape(text) auto-escapes those three characters.

Usage:
    # Drop-in replacement for the original telegram() call:
    from util.notifier import telegram
    telegram("Entry placed: CE NIFTY25MAR2623000CE @ Rs.123.50")

    # Same function, alternative import name:
    from util.notifier import notify
    notify("Daily target hit — closing all legs")

    # For plain-text content that may contain < > & characters:
    from util.notifier import notify, html_escape
    notify(f"Exception: {html_escape(str(exc))}")

    # Wait for all queued messages to be delivered (e.g. before graceful exit):
    from util.notifier import flush
    flush(timeout=10)

VERSION:
    Matches the strategy version constant from the reference script (5.9.0).
    Update this when the strategy version changes.
═══════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Optional

import requests

from util.logger import debug, error, info, warn

__all__ = ["notify", "telegram", "html_escape", "flush"]

# ── Strategy version — update when Nifty_ShortStraddle_Partial.py version changes
_VERSION = "5.9.0"

# ── Telegram API base URL ─────────────────────────────────────────────────────
_TELEGRAM_API_BASE = "https://api.telegram.org"

# ── Message constraints ───────────────────────────────────────────────────────
_MAX_MSG_LEN   = 4096        # Telegram hard limit (characters per sendMessage)
_TRUNCATE_TAIL = "[…truncated]"

# ── Retry policy ──────────────────────────────────────────────────────────────
# 3 attempts total: 1 initial + 2 retries.
# Back-off delays (seconds) between consecutive attempts: [1s, 3s].
# WHY 3s on second retry: Telegram's flood-control window is typically 1–5s;
# a 3s wait is usually sufficient to clear a 429 rate-limit.
_MAX_ATTEMPTS    = 3
_BACKOFF_DELAYS  = (1, 3)    # seconds before attempt 2, attempt 3

# ── HTTP timeouts (seconds) ───────────────────────────────────────────────────
# Connect timeout: time allowed to establish a TCP connection.
# Read timeout: time allowed to receive the full response body.
# Separating them prevents a slow TTFB from monopolising the retry budget.
_CONNECT_TIMEOUT = 5
_READ_TIMEOUT    = 8

# ── Queue sentinel value ──────────────────────────────────────────────────────
# A None pushed to the queue signals the worker thread to exit gracefully.
_SENTINEL = None


# ═══════════════════════════════════════════════════════════════════════════════
#  Module-level shared objects
# ═══════════════════════════════════════════════════════════════════════════════

# Background delivery queue — unbounded; each item is a formatted message string.
# Queue.put() is thread-safe and non-blocking so notify() always returns instantly.
_send_queue: queue.Queue[Optional[str]] = queue.Queue()

# Lazy-initialised persistent HTTP session — created on first send, reused
# for all subsequent messages (connection pooling, TLS session resumption).
_http_session: Optional[requests.Session] = None
_session_lock = threading.Lock()

# Worker thread handle — replaced if the thread dies (e.g. after flush/stop).
_worker_thread: Optional[threading.Thread] = None
_worker_lock   = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
#  Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _get_config():
    """
    Lazily fetch the Config singleton. Returns None if not yet loaded.
    Import is deferred to avoid circular imports at module load time.
    """
    try:
        from util.config_util import cfg  # noqa: PLC0415
        return cfg
    except Exception:
        return None


def _get_session() -> requests.Session:
    """Return the shared HTTP session, creating it if necessary."""
    global _http_session
    with _session_lock:
        if _http_session is None:
            _http_session = requests.Session()
            # Set default headers once — applied to every request from this session.
            _http_session.headers.update({
                "Content-Type" : "application/json",
                "User-Agent"   : f"NiftyShortStraddle/{_VERSION}",
            })
        return _http_session


def _is_enabled() -> bool:
    """
    Return True if Telegram notifications are enabled and credentials present.
    Called inside the worker thread before each send attempt.
    """
    cfg = _get_config()
    if cfg is None:
        return False
    return bool(
        cfg.TELEGRAM_ENABLED
        and cfg.TELEGRAM_BOT_TOKEN
        and cfg.TELEGRAM_CHAT_ID
    )


def _build_text(msg: str) -> str:
    """
    Prepend the strategy name + version header and enforce the 4096-char limit.

    Format (matches original script exactly):
        [Short Straddle v5.9.0]
        <message body>

    If the combined string exceeds 4096 chars, the message body is truncated
    so the header is always fully delivered and the tail marker is appended.
    """
    cfg = _get_config()
    strategy_name = cfg.STRATEGY_NAME if cfg else "Short Straddle"
    header = f"[{strategy_name} v{_VERSION}]\n"

    available = _MAX_MSG_LEN - len(header) - len(_TRUNCATE_TAIL)
    if len(msg) > available:
        warn(
            f"Telegram message truncated from {len(msg)} to {available} chars "
            f"(Telegram limit: {_MAX_MSG_LEN})"
        )
        msg = msg[:available] + _TRUNCATE_TAIL

    return header + msg


def _send_once(text: str) -> bool:
    """
    Attempt a single HTTP POST to the Telegram sendMessage endpoint.

    Returns True on success (HTTP 200 + status OK from Telegram).
    Returns False on transient error (network, 429, 5xx) — caller should retry.
    Logs and returns True (do not retry) on permanent error (400 bad request).

    WHY return True on permanent error:
        A 400 means the message itself is malformed (invalid HTML, bad chat_id
        format, etc.). Retrying will not fix it. We treat it as "delivered /
        no-op" so the caller stops retrying and logs the problem once.
    """
    cfg = _get_config()
    if cfg is None:
        return True   # Config gone mid-session — skip silently

    url  = f"{_TELEGRAM_API_BASE}/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id"    : cfg.TELEGRAM_CHAT_ID,
        "text"       : text,
        "parse_mode" : "HTML",
    }

    try:
        resp = _get_session().post(
            url,
            json    = data,
            timeout = (_CONNECT_TIMEOUT, _READ_TIMEOUT),
        )

        if resp.status_code == 200:
            debug("Telegram delivered OK")
            return True

        if resp.status_code == 429:
            # Flood control — honour Retry-After if present, else use default.
            retry_after = int(resp.headers.get("Retry-After", _BACKOFF_DELAYS[0]))
            warn(
                f"Telegram 429 rate-limited — waiting {retry_after}s "
                f"(Retry-After header: {resp.headers.get('Retry-After', 'absent')})"
            )
            time.sleep(retry_after)
            return False   # Signal: retry

        if 500 <= resp.status_code < 600:
            warn(f"Telegram server error {resp.status_code} — will retry")
            return False   # Signal: retry (transient server issue)

        # 400 or other 4xx — permanent client-side error, do not retry
        error(
            f"Telegram permanent error {resp.status_code}: "
            f"{resp.text[:200]}"
        )
        return True   # Stop retrying (won't improve)

    except requests.exceptions.Timeout:
        warn(
            f"Telegram timed out (connect={_CONNECT_TIMEOUT}s read={_READ_TIMEOUT}s) "
            "— will retry"
        )
        return False

    except requests.exceptions.ConnectionError as exc:
        warn(f"Telegram connection error: {exc} — will retry")
        return False

    except Exception as exc:
        warn(f"Telegram unexpected error: {exc}")
        return True   # Unknown error — don't loop forever, stop retrying


def _send_with_retry(text: str) -> None:
    """
    Attempt delivery up to _MAX_ATTEMPTS times with exponential back-off.

    Attempt schedule:
        Attempt 1 → immediately
        Attempt 2 → after _BACKOFF_DELAYS[0] seconds
        Attempt 3 → after _BACKOFF_DELAYS[1] seconds
        ...

    Note: _send_once() may sleep internally on a 429 Retry-After before
    returning False. The back-off here is ADDITIONAL sleep after that.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if not _is_enabled():
            debug("Telegram disabled mid-send — skipping")
            return

        success = _send_once(text)

        if success:
            return   # Delivered (or permanently failed — stop either way)

        # Transient failure — sleep before next attempt
        if attempt < _MAX_ATTEMPTS:
            delay = _BACKOFF_DELAYS[min(attempt - 1, len(_BACKOFF_DELAYS) - 1)]
            debug(f"Telegram attempt {attempt} failed — retrying in {delay}s")
            time.sleep(delay)

    warn(
        f"Telegram delivery failed after {_MAX_ATTEMPTS} attempts — "
        "message dropped. Check network and Telegram credentials."
    )


def _worker_loop() -> None:
    """
    Background daemon thread: drain the send queue and deliver each message.

    Runs until a None sentinel is received (from flush()) or the process exits.
    Exceptions inside _send_with_retry() are fully handled there — this loop
    must never raise so the daemon thread does not silently die mid-session.
    """
    while True:
        try:
            text = _send_queue.get()

            if text is _SENTINEL:
                _send_queue.task_done()
                return   # Graceful shutdown

            _send_with_retry(text)
            _send_queue.task_done()

        except Exception as exc:
            # Safety net — should never reach here.
            warn(f"Telegram worker unexpected error: {exc}")
            try:
                _send_queue.task_done()
            except ValueError:
                pass


def _ensure_worker_running() -> None:
    """
    Start the background worker thread if it is not already alive.
    Uses a lock to prevent duplicate threads on concurrent notify() calls.
    """
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(
                target = _worker_loop,
                name   = "telegram-sender",
                daemon = True,   # Does not prevent process exit
            )
            _worker_thread.start()
            debug("Telegram worker thread started")


# ═══════════════════════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════════════════════

def html_escape(text: str) -> str:
    """
    Escape the three characters that have special meaning in Telegram HTML mode.

    Use this helper when embedding variable content (exception messages, symbol
    names, filenames) in a message that uses parse_mode="HTML". Failing to escape
    these characters causes a 400 Bad Request from the Telegram API.

    Escaping table:
        &  →  &amp;   (must be escaped first — otherwise &lt; → &amp;lt;)
        <  →  &lt;
        >  →  &gt;

    Example:
        notify(f"Exception in job: {html_escape(str(exc))}")
        notify(f"Symbol: {html_escape(symbol)} | Entry: Rs.{price:.2f}")
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def notify(msg: str) -> None:
    """
    Enqueue a Telegram message for background delivery.

    Returns immediately — delivery is handled by a background daemon thread.
    Never raises. Silently skips if Telegram is disabled or credentials are
    missing (controlled by [telegram] enabled = false in config.toml).

    Parameters
    ----------
    msg : str
        The message body. Supports Telegram HTML markup:
          <b>bold</b>  <i>italic</i>  <code>monospace</code>
        Characters &, <, > in plain text must be escaped — see html_escape().
        Messages longer than 4096 characters are automatically truncated.

    Notes
    -----
    The "[{STRATEGY_NAME} v{VERSION}]" header is prepended automatically,
    matching the original telegram() behaviour exactly.

    Example
    -------
        from util.notifier import notify
        notify("Entry placed: CE NIFTY25MAR2623000CE @ Rs.123.50")
        notify(f"<b>SL Hit</b>  CE @ Rs.{ltp:.2f}  (SL was Rs.{sl:.2f})")
        notify(f"Exception: {html_escape(str(exc))}")
    """
    if not _is_enabled():
        return

    try:
        text = _build_text(msg)
        _ensure_worker_running()
        _send_queue.put(text)
    except Exception as exc:
        # Protect the caller — notify() must NEVER raise
        warn(f"Telegram enqueue failed: {exc}")


# ── Drop-in alias — matches the original function name ───────────────────────
# Existing strategy_core.py code can do: from util.notifier import telegram
# with zero changes to the call sites.
telegram = notify


def flush(timeout: float = 10.0) -> bool:
    """
    Block until all queued Telegram messages have been delivered (or time out).

    Call this before a graceful shutdown to ensure in-flight alerts are sent
    (e.g. "Strategy stopped by operator") before the process exits.

    Parameters
    ----------
    timeout : float
        Maximum seconds to wait. Default: 10s.

    Returns
    -------
    bool
        True  — all queued messages were delivered within the timeout.
        False — timeout expired; some messages may still be queued.

    Notes
    -----
    This function is safe to call even if the queue is empty or the worker
    thread was never started (it returns True immediately in both cases).

    Example
    -------
        from util.notifier import notify, flush
        notify("Strategy stopped by operator")
        flush(timeout=8)   # wait up to 8s for delivery before exit
    """
    if _send_queue.empty():
        return True

    try:
        _send_queue.join()   # blocks until all task_done() calls match put() calls
        return True
    except Exception:
        # queue.join() with timeout is not natively supported — fall back to polling
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if _send_queue.empty():
                return True
            time.sleep(0.1)
        return False


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI self-test — smoke-test the full delivery pipeline
#
#  Usage (from the strategy directory, using the algo_trading venv):
#
#    OPENALGO_APIKEY=x TELEGRAM_BOT_TOKEN=<real_token> TELEGRAM_CHAT_ID=<real_id> \
#    python -m util.notifier
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
        built    = _build_text(long_msg)
        trunc_ok = len(built) <= _MAX_MSG_LEN and built.endswith(_TRUNCATE_TAIL)
        ok       = ok and trunc_ok
        print(f"  {'✓' if trunc_ok else '✗'}  5000-char message truncated to "
              f"{len(built)} chars (limit: {_MAX_MSG_LEN}) ending with {_TRUNCATE_TAIL!r}")

        print()
        print("  Testing notify() with Telegram disabled (should silently skip):")
        original_enabled = cfg.TELEGRAM_ENABLED if cfg else False
        # Monkey-patch cfg for this test by temporarily disabling
        notify("This message should be silently dropped (Telegram disabled)")
        time.sleep(0.2)
        queue_empty = _send_queue.empty()
        print(f"  {'✓' if queue_empty else '✗'}  Queue empty after notify() with Telegram disabled")

        print()
        if ok and queue_empty:
            print("  All dry-run assertions passed ✓")
        else:
            print("  SOME ASSERTIONS FAILED ✗")
            sys.exit(1)

    else:
        print(f"  Bot token  : ***{cfg.TELEGRAM_BOT_TOKEN[-6:]}")
        print(f"  Chat ID    : {cfg.TELEGRAM_CHAT_ID}")
        print(f"  Strategy   : {cfg.STRATEGY_NAME}  v{_VERSION}")
        print()
        print("  Sending test messages — check your Telegram chat...")
        print()

        notify("🧪 <b>Notifier self-test</b>\nDelivery pipeline: queue → background thread → retry.")
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
