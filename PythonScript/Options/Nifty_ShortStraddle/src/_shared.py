"""
src/_shared.py  —  Shared constants, lazy client, and utility helpers
═══════════════════════════════════════════════════════════════════════
Everything in this module is used by two or more other src/ modules.
Import this instead of duplicating constants or helpers.

Exports:
  VERSION, IST, OPTION_EXCH, INDEX_EXCH, VIX_SYMBOL   — strategy constants
  DAY_NAMES, MONTH_NAMES                               — display helpers
  _monitor_lock, _last_vix_spike_check_time, ...       — monitor thread vars
  _get_client()                                        — lazy OpenAlgo client
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
#  MODULE-LEVEL THREAD STATE
#
#  Intentionally module-level (not instance attributes) so that each
#  APScheduler job closure and background thread shares the SAME counters
#  across calls — even when accessed through different StrategyCore method
#  calls.  Using instance attributes would require passing `self` into every
#  daemon thread and scheduler job, adding unnecessary coupling.
#
#  _monitor_lock        : RLock — serialises monitor ticks with close ops.
#                         Reentrant so close_all() → close_one_leg() is safe.
#  _last_vix_spike_*    : throttle — VIX spike check runs at most once per
#                         VIX_SPIKE_CHECK_INTERVAL_S seconds.
#  _last_spot_check_*   : throttle — spot-move check runs at most once per
#                         SPOT_CHECK_INTERVAL_S seconds.
#  _first_tick_fired    : True once the first monitor tick fires after entry.
#                         Guards the 0–15s window where today_pnl == 0 is real.
#  _consecutive_*       : escalation counters — alert after N consecutive fails.
# ═══════════════════════════════════════════════════════════════════════════════

_monitor_lock:                 threading.RLock = threading.RLock()
_last_vix_spike_check_time:    Optional[datetime] = None
_last_spot_check_time:         Optional[datetime] = None
_first_tick_fired:             bool = False
_consecutive_quote_fail_ticks: int  = 0
_quote_fail_alerted:           bool = False
_consecutive_monitor_skips:    int  = 0


# ═══════════════════════════════════════════════════════════════════════════════
#  LAZY OPENALGO CLIENT
#
#  Initialised on first use (not at import time) so that:
#    • Unit tests can import without a live broker connection.
#    • config_util finishes loading before the client is constructed.
#    • A config reload automatically picks up the new API key.
# ═══════════════════════════════════════════════════════════════════════════════

_client_instance: Optional[OpenAlgoClient] = None
_client_lock = threading.Lock()


def _get_client() -> OpenAlgoClient:
    """Return the singleton OpenAlgoClient, constructing it on first call."""
    global _client_instance
    if _client_instance is None:
        with _client_lock:
            if _client_instance is None:   # double-checked locking
                _client_instance = OpenAlgoClient(
                    api_key = cfg.OPENALGO_API_KEY,
                    host    = cfg.OPENALGO_HOST,
                )
    return _client_instance


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
