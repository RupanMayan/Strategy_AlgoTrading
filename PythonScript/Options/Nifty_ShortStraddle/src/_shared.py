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
  sl_level(), _dynamic_sl_percent()                    — SL helpers
  telegram                                             — notify alias
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

# ── stdlib ────────────────────────────────────────────────────────────────────
import threading
from datetime import datetime
from typing import Optional

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

VERSION     = "5.9.0"
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
        self._instance: Optional[OpenAlgoClient] = None
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
        self.last_vix_spike_check_time: Optional[datetime] = None
        self.last_spot_check_time:      Optional[datetime] = None
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

# ── Backward-compatible aliases for code that accesses _shared.xxx directly ──
# These are properties bridged to the MonitorState singleton so that existing
# code like `_shared._last_vix_spike_check_time = now` continues to work.
# We use module-level attribute access via __getattr__/__setattr__ overrides
# defined at the bottom of this file.


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
    result = []
    if state["ce_active"]:
        result.append("CE")
    if state["pe_active"]:
        result.append("PE")
    return result


def _dynamic_sl_percent() -> float:
    """
    Return the effective per-leg SL % based on current time.

    When DYNAMIC_SL_ENABLED = True, DYNAMIC_SL_SCHEDULE is evaluated in
    descending time order — the first entry whose threshold <= current HH:MM
    wins.  Falls back to LEG_SL_PERCENT if no entry matches.

    When DYNAMIC_SL_ENABLED = False, LEG_SL_PERCENT is always returned.
    """
    if not cfg.DYNAMIC_SL_ENABLED:
        return cfg.LEG_SL_PERCENT
    now_hm = now_ist().strftime("%H:%M")
    for threshold, sl_pct in cfg.DYNAMIC_SL_SCHEDULE:
        if now_hm >= threshold:
            return sl_pct
    return cfg.LEG_SL_PERCENT


def sl_level(leg: str) -> float:
    """
    Return the CURRENT effective SL trigger price for a given leg.

    Priority (highest → lowest):
      1. Trailing SL  — if TRAILING_SL_ENABLED and activated for this leg
      2. Breakeven SL — if BREAKEVEN_AFTER_PARTIAL_ENABLED and activated,
                        and the breakeven price is TIGHTER than fixed SL
      3. Fixed SL     — entry_price × (1 + _dynamic_sl_percent() / 100)

    Returns 0.0 when entry price is not yet captured (SL check is skipped).
    """
    leg_lower = leg.lower()
    entry = state[f"entry_price_{leg_lower}"]
    if entry <= 0:
        return 0.0

    # 1. Trailing SL takes highest priority once activated
    if cfg.TRAILING_SL_ENABLED and state.get(f"trailing_active_{leg_lower}", False):
        trail_sl = state.get(f"trailing_sl_{leg_lower}", 0.0)
        if trail_sl > 0:
            return trail_sl

    # 2. Breakeven SL — only when it is tighter (lower) than fixed SL
    if cfg.BREAKEVEN_AFTER_PARTIAL_ENABLED and state.get(f"breakeven_active_{leg_lower}", False):
        be_sl    = state.get(f"breakeven_sl_{leg_lower}", 0.0)
        fixed_sl = round(entry * (1.0 + _dynamic_sl_percent() / 100.0), 2)
        if be_sl > 0 and be_sl < fixed_sl:
            return be_sl

    # 3. Fixed SL (time-graduated when DYNAMIC_SL_ENABLED)
    return round(entry * (1.0 + _dynamic_sl_percent() / 100.0), 2)


# ═══════════════════════════════════════════════════════════════════════════════
#  Module __getattr__ / __setattr__ — backward-compatible attribute bridging
#
#  Other modules access monitor state via:
#    _shared._last_vix_spike_check_time = now
#    _shared._first_tick_fired = True
#    _shared._consecutive_quote_fail_ticks += 1
#    etc.
#
#  These are bridged to _monitor_state attributes so the MonitorState class
#  owns the data while existing code works without modification.
# ═══════════════════════════════════════════════════════════════════════════════

_MONITOR_STATE_ALIASES = {
    "_last_vix_spike_check_time":    "last_vix_spike_check_time",
    "_last_spot_check_time":         "last_spot_check_time",
    "_first_tick_fired":             "first_tick_fired",
    "_consecutive_quote_fail_ticks": "consecutive_quote_fail_ticks",
    "_quote_fail_alerted":           "quote_fail_alerted",
    "_consecutive_monitor_skips":    "consecutive_monitor_skips",
}


def __getattr__(name: str):
    """Module-level __getattr__ for backward-compatible monitor state access."""
    if name in _MONITOR_STATE_ALIASES:
        return getattr(_monitor_state, _MONITOR_STATE_ALIASES[name])
    raise AttributeError(f"module 'src._shared' has no attribute {name!r}")
