"""
src/_shared.py  —  Shared constants, lazy client, and utility helpers
═══════════════════════════════════════════════════════════════════════
Everything in this module is used by two or more other src/ modules.
Import this instead of duplicating constants or helpers.

Exports:
  VERSION, IST, OPTION_EXCH, INDEX_EXCH, VIX_SYMBOL   — strategy constants
  DAY_NAMES, MONTH_NAMES                               — display helpers
  BrokerClient, _broker_client, _get_client()          — lazy OpenAlgo client
  MonitorState, _monitor_state                         — monitor thread vars
  _monitor_lock                                        — monitor RLock (alias)
  now_ist(), qty(), parse_hhmm(), active_legs()        — stateless helpers
  fetch_ltp()                                          — broker quote helper
  sl_level(), _dynamic_sl_percent()                    — SL helpers
  telegram                                             — notify alias
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

# ── stdlib ────────────────────────────────────────────────────────────────────
import threading
from datetime import datetime

# ── third-party ───────────────────────────────────────────────────────────────
import pytz
from openalgo import api as OpenAlgoClient

# ── strategy modules ──────────────────────────────────────────────────────────
from util.config_util import cfg
from util.logger import info, warn, error, debug, sep
from util.notifier import notify
from util.state import state, save_state, load_state, clear_state_file

# Drop-in alias so all call sites inside this package read naturally
telegram = notify


# ═══════════════════════════════════════════════════════════════════════════════
#  INTERNAL CONSTANTS
#  Strategy-level fixed values — not user-configurable.
#  Change user-facing settings in config.toml only.
# ═══════════════════════════════════════════════════════════════════════════════

VERSION     = "7.1.0"
IST         = pytz.timezone("Asia/Kolkata")
OPTION_EXCH = "NFO"        # All F&O option contracts (quotes / positions)
INDEX_EXCH  = "NSE_INDEX"  # Underlying index + VIX (order entry)
VIX_SYMBOL  = "INDIAVIX"   # OpenAlgo symbol format — docs.openalgo.in

DAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]
MONTH_NAMES = {
    1: "January",   2: "February", 3: "March",    4: "April",
    5: "May",       6: "June",     7: "July",      8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  BrokerClient — thread-safe lazy OpenAlgo client singleton
#
#  Initialised on first use (not at import time) so that:
#    • Unit tests can import without a live broker connection.
#    • config_util finishes loading before the client is constructed.
#    • A config reload automatically picks up the new API key.
# ═══════════════════════════════════════════════════════════════════════════════

class BrokerClient:
    """
    Thread-safe lazy singleton for the OpenAlgo API client.

    The client is constructed on the first call to get_client(), not at import
    time. Double-checked locking ensures exactly one instance across threads.
    """

    def __init__(self) -> None:
        self._instance: OpenAlgoClient | None = None
        self._lock = threading.Lock()

    def get_client(self) -> OpenAlgoClient:
        """Return the singleton OpenAlgoClient, constructing it on first call."""
        if self._instance is None:
            with self._lock:
                if self._instance is None:   # double-checked locking
                    self._instance = OpenAlgoClient(
                        api_key = cfg.OPENALGO_API_KEY,
                        host    = cfg.OPENALGO_HOST,
                    )
        return self._instance


# Module-level singleton
_broker_client = BrokerClient()


def _get_client() -> OpenAlgoClient:
    """Backward-compatible wrapper — delegates to the BrokerClient singleton."""
    return _broker_client.get_client()


# ═══════════════════════════════════════════════════════════════════════════════
#  MonitorState — mutable thread state shared across monitor/order modules
#
#  Encapsulates all the counters and timestamps that coordinate the monitor
#  loop, VIX spike check throttle, spot-move check throttle, and broker
#  connectivity escalation.
#
#  A single module-level instance (_monitor_state) is used by all modules
#  via `import src._shared as _shared; _shared._monitor_state.xxx`.
#
#  _monitor_lock (RLock) is intentionally kept as a separate module-level
#  variable since it is imported directly by name in monitor.py and
#  order_engine.py: `from src._shared import _monitor_lock`.
# ═══════════════════════════════════════════════════════════════════════════════

class MonitorState:
    """
    Mutable state shared between monitor ticks, close operations, and
    background threads.

    Attributes
    ----------
    last_vix_spike_check_time : throttle for VIX spike check
    last_spot_check_time      : throttle for spot-move check
    first_tick_fired          : True once the first monitor tick fires after entry
    consecutive_quote_fail_ticks : escalation counter for LTP failures
    quote_fail_alerted        : True once the failure alert has been sent
    consecutive_monitor_skips : lock-contention skip counter
    """

    def __init__(self) -> None:
        self.last_vix_spike_check_time: datetime | None = None
        self.last_spot_check_time:      datetime | None = None
        self.first_tick_fired:          bool = False
        self.consecutive_quote_fail_ticks: int  = 0
        self.quote_fail_alerted:        bool = False
        self.consecutive_monitor_skips: int  = 0

    def reset_entry(self) -> None:
        """Reset counters at new position entry."""
        self.first_tick_fired          = False
        self.consecutive_quote_fail_ticks = 0
        self.quote_fail_alerted        = False


# Module-level singleton
_monitor_state = MonitorState()

# RLock exposed as a module-level variable for backward-compatible direct import.
# Reentrant so close_all() → close_one_leg() is safe.
_monitor_lock: threading.RLock = threading.RLock()

# ═══════════════════════════════════════════════════════════════════════════════
#  MODULE-LEVEL UTILITY HELPERS
#
#  Pure, stateless helpers used throughout ALL classes.
#  Defined here (not as class methods) so they can be called without a class
#  instance — from background threads, scheduler jobs, and tests.
# ═══════════════════════════════════════════════════════════════════════════════

def now_ist() -> datetime:
    """Return current datetime in IST (Asia/Kolkata, UTC+05:30)."""
    return datetime.now(IST)


def qty() -> int:
    """Total quantity per leg = NUMBER_OF_LOTS × LOT_SIZE."""
    return cfg.NUMBER_OF_LOTS * cfg.LOT_SIZE


def parse_hhmm(t: str) -> tuple[int, int]:
    """Parse 'HH:MM' string → (hour: int, minute: int)."""
    h, m = t.strip().split(":")
    return int(h), int(m)


def active_legs() -> list[str]:
    """
    Return list of currently open leg identifiers.

    Possible values: ['CE', 'PE'] | ['CE'] | ['PE'] | []
    Reads directly from the shared state singleton.
    """
    return [leg for leg, key in (("CE", "ce_active"), ("PE", "pe_active")) if state[key]]


def _base_sl_percent() -> float:
    """
    Return the base per-leg SL % — DTE-aware if configured.

    Priority:
      1. DTE_SL_OVERRIDE[current_dte] — if an override exists for today's DTE
      2. LEG_SL_PERCENT              — default base SL

    This is the "morning" SL that dynamic time-of-day tightening acts on.
    """
    if cfg.DTE_SL_OVERRIDE:
        dte = state.get("current_dte")
        if dte is not None and dte in cfg.DTE_SL_OVERRIDE:
            return cfg.DTE_SL_OVERRIDE[dte]
    return cfg.LEG_SL_PERCENT


def _dynamic_sl_percent() -> float:
    """
    Return the effective per-leg SL % based on DTE + current time.

    Evaluation order:
      1. Base SL = _base_sl_percent() (DTE-override or LEG_SL_PERCENT)
      2. If DYNAMIC_SL_ENABLED, time-of-day schedule can TIGHTEN below base.
         Each schedule entry's sl_pct is used as-is (not relative to base).
         Only entries with sl_pct < base are effective tightenings.

    When DYNAMIC_SL_ENABLED = False, _base_sl_percent() is returned.
    """
    base = _base_sl_percent()
    if not cfg.DYNAMIC_SL_ENABLED:
        return base
    now_hm = now_ist().strftime("%H:%M")
    for threshold, sl_pct in cfg.DYNAMIC_SL_SCHEDULE:
        if now_hm >= threshold:
            # Only tighten — never widen beyond the base
            return min(sl_pct, base)
    return base


def parse_ist_datetime(raw: str | datetime | None) -> datetime | None:
    """
    Parse a raw value (ISO string or datetime) into an IST-aware datetime.

    Returns None if parsing fails or raw is None/empty.
    Used across modules to avoid duplicating datetime parsing + IST localization.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else IST.localize(raw)
    try:
        parsed = datetime.fromisoformat(str(raw))
        return parsed if parsed.tzinfo else IST.localize(parsed)
    except (ValueError, TypeError):
        return None


def fetch_ltp(symbol: str, exchange: str) -> float:
    """
    Fetch the last traded price for a symbol via OpenAlgo quotes API.

    Returns the LTP as a float, or 0.0 if the call fails or the response
    is invalid. Exceptions are caught and logged — callers never need to
    wrap this in try/except.
    """
    try:
        resp = _get_client().quotes(symbol=symbol, exchange=exchange)
        if is_api_success(resp):
            ltp = float(resp.get("data", {}).get("ltp", 0) or 0)
            return ltp if ltp > 0 else 0.0
        debug(f"fetch_ltp({symbol}): API error — {get_api_error(resp)}")
    except Exception as exc:
        debug(f"fetch_ltp({symbol}): exception — {exc}")
    return 0.0


def is_api_success(resp: object) -> bool:
    """Check if an OpenAlgo API response indicates success."""
    return isinstance(resp, dict) and resp.get("status") == "success"


def get_api_error(resp: object) -> str:
    """Extract error message from a failed OpenAlgo API response."""
    if isinstance(resp, dict):
        return resp.get("message", str(resp))
    return str(resp)


def sl_level(leg: str) -> float:
    """
    Return the CURRENT effective SL trigger price for a given leg.

    Priority (highest → lowest):
      1. Trailing SL  — if TRAILING_SL_ENABLED and activated for this leg
      2. Breakeven SL — if BREAKEVEN_AFTER_PARTIAL_ENABLED and activated,
                        AND grace period has elapsed,
                        and the breakeven price is TIGHTER than fixed SL
      3. Fixed SL     — entry_price × (1 + _dynamic_sl_percent() / 100)

    Returns 0.0 when entry price is not yet captured (SL check is skipped).
    """
    leg_lower = leg.lower()
    entry = state[f"entry_price_{leg_lower}"]
    if entry <= 0:
        # FIX-XIX: Fallback SL when fill price is unknown (fill capture failed).
        # Use the other leg's entry price as a proxy — ATM straddle legs have
        # near-identical premium. This prevents the leg from running with NO SL
        # protection for the entire session.
        other_leg = "ce" if leg_lower == "pe" else "pe"
        other_entry = state.get(f"entry_price_{other_leg}", 0.0)
        if other_entry > 0:
            fallback_sl = round(other_entry * (1.0 + _dynamic_sl_percent() / 100.0), 2)
            warn(
                f"sl_level({leg}): entry_price=0 — using other leg's entry "
                f"Rs.{other_entry:.2f} as proxy → fallback SL Rs.{fallback_sl:.2f}"
            )
            return fallback_sl
        return 0.0  # Both legs have no fill price — truly no SL possible

    # 1. Trailing SL takes highest priority once activated
    if cfg.TRAILING_SL_ENABLED and state.get(f"trailing_active_{leg_lower}", False):
        trail_sl = state.get(f"trailing_sl_{leg_lower}", 0.0)
        if trail_sl > 0:
            return trail_sl

    # 2. Breakeven SL — only when tighter than fixed SL AND grace period has elapsed
    fixed_sl = round(entry * (1.0 + _dynamic_sl_percent() / 100.0), 2)

    if cfg.BREAKEVEN_AFTER_PARTIAL_ENABLED and state.get(f"breakeven_active_{leg_lower}", False):
        be_sl = state.get(f"breakeven_sl_{leg_lower}", 0.0)
        if be_sl > 0 and be_sl < fixed_sl:
            # Grace period gate: apply breakeven SL only after the grace window
            # has elapsed. During the grace window, the normal fixed/dynamic SL
            # protects the leg, giving it time to decay organically.
            if not cfg.BREAKEVEN_GRACE_PERIOD_MIN:
                return be_sl  # No grace period configured — apply immediately

            be_activated_at = parse_ist_datetime(
                state.get(f"breakeven_activated_at_{leg_lower}")
            )
            if be_activated_at is None:
                return be_sl  # No timestamp — fail-safe to breakeven

            elapsed_min = (now_ist() - be_activated_at).total_seconds() / 60.0
            if elapsed_min >= cfg.BREAKEVEN_GRACE_PERIOD_MIN:
                return be_sl  # Grace period elapsed — breakeven SL active
            # Grace period still active — fall through to fixed SL

    # 3. Fixed SL (time-graduated when DYNAMIC_SL_ENABLED, DTE-aware via _base_sl_percent)
    return fixed_sl


