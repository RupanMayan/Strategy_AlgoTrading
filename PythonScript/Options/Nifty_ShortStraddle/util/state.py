"""
util/state.py  —  In-memory state + crash-safe persistence for Nifty Short Straddle
═══════════════════════════════════════════════════════════════════════════════════════
Responsibilities:
  1. Define the canonical initial state (all keys + their zero/default values)
  2. Expose a module-level mutable singleton (`state`) so all modules share
     the same object in memory — mutations are visible immediately everywhere
  3. Persist to JSON atomically (temp-file + os.replace) — partial writes can
     NEVER corrupt the live state file even on power loss or SIGKILL
  4. Load saved state from disk on restart with full type restoration:
       entry_time  : ISO string  → tz-aware datetime (IST)
       entry_date  : str/None   → str/None  (left as-is, used for staleness check)
       All numeric, bool, and string fields are returned as-is from json.load()
  5. Provide reset_state() to wipe the live dict back to blank values without
     replacing the object (so existing `from util.state import state` references
     in other modules continue to point to the live dict)

State dict key groups:
  • Position flags      — in_position, ce_active, pe_active
  • Leg symbols         — symbol_ce, symbol_pe
  • Order IDs           — orderid_ce, orderid_pe
  • Entry fill prices   — entry_price_ce, entry_price_pe
  • Exit fill prices    — exit_price_ce, exit_price_pe
  • Realised P&L        — closed_pnl
  • Trailing SL         — trailing_active_ce/pe, trailing_sl_ce/pe
  • Breakeven SL        — breakeven_active_ce/pe, breakeven_sl_ce/pe
  • Opening range       — orb_price
  • Entry context       — underlying_ltp, vix_at_entry, ivr_at_entry, ivp_at_entry,
                          entry_time, entry_date, margin_required, margin_available
  • Running P&L         — today_pnl
  • Session stats       — trade_count, exit_reason

Persistence design:
  • Atomic write: write to a temp file in the same directory, then os.replace() —
    on Linux/Mac rename() is atomic; on Windows os.replace() is atomic on NTFS.
    A crash at ANY point (between write and rename, mid-write) leaves the previous
    state file intact — no corrupt state is ever visible to the reader.
  • Encoding: UTF-8 JSON, 2-space indented — human-readable, easy to inspect
    with `cat strategy_state.json` in a shell.
  • Caller model: save_state() is called after EVERY state mutation in strategy_core,
    typically 2–3 times per entry and once per monitor tick that closes a leg.

Usage:
    # Import the live singleton and persistence helpers:
    from util.state import state, save_state, load_state, clear_state_file, reset_state

    # Read / mutate in-place (same object visible to all modules):
    state["ce_active"] = True
    state["entry_price_ce"] = 123.50
    save_state()

    # On startup — load and merge into live dict:
    saved = load_state()
    if saved:
        state.update(saved)

    # After position is fully flat:
    clear_state_file()
    reset_state()

    # For tests — use an explicit path:
    save_state(state_path="/tmp/test_state.json")
    loaded = load_state(state_path="/tmp/test_state.json")

═══════════════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pytz

from util.logger import debug, error, info, warn

__all__ = [
    "INITIAL_STATE",
    "state",
    "save_state",
    "load_state",
    "clear_state_file",
    "reset_state",
]

# ── IST timezone ──────────────────────────────────────────────────────────────
_IST = pytz.timezone("Asia/Kolkata")


# ═══════════════════════════════════════════════════════════════════════════════
#  INITIAL_STATE — canonical blank state
#
#  All 30 keys are listed here with their zero / empty default values.
#  This serves as:
#    1. Documentation of every field, its type, and its meaning
#    2. The source for reset_state() — every key is reset to its value here
#    3. A reference for what load_state() must restore on restart
#
#  IMPORTANT: Do NOT modify this dict at runtime. It is used exclusively
#  as a template — the live singleton is a separate copy.
# ═══════════════════════════════════════════════════════════════════════════════

INITIAL_STATE: dict[str, Any] = {
    # ── Position flags ────────────────────────────────────────────────────────
    # in_position: True if ANY leg is still open (CE or PE or both)
    # ce_active / pe_active: True = leg is currently open at the broker
    "in_position"         : False,

    "ce_active"           : False,   # True = CE sell position exists at broker
    "pe_active"           : False,   # True = PE sell position exists at broker

    # ── Leg symbols ───────────────────────────────────────────────────────────
    # Resolved by OpenAlgo from ATM strike + expiry at entry time.
    # Format: "NIFTY25MAR2623000CE", "NIFTY25MAR2623000PE"
    "symbol_ce"           : "",
    "symbol_pe"           : "",

    # ── Order IDs ─────────────────────────────────────────────────────────────
    # Returned by place_order() — used by orderstatus() to fetch fill prices.
    "orderid_ce"          : "",
    "orderid_pe"          : "",

    # ── Entry fill prices — basis of ALL SL calculations ─────────────────────
    # Fetched via orderstatus() after entry (not the requested price).
    # Fixed permanently at entry — SL level = entry_price × (1 + SL%/100).
    # 0.0 = fill not yet captured (crash guard: SL skipped when entry=0).
    "entry_price_ce"      : 0.0,
    "entry_price_pe"      : 0.0,

    # ── Exit fill prices — recorded by close_one_leg() for trade log ──────────
    # Actual broker average fill if orderstatus() available; else trigger LTP.
    "exit_price_ce"       : 0.0,
    "exit_price_pe"       : 0.0,

    # ── Realised P&L from legs already closed this session ───────────────────
    # closed_pnl += (entry_price - exit_price) × qty  when a leg closes.
    # combined_pnl = closed_pnl + open_leg(s)_mtm  — used for target/limit.
    "closed_pnl"          : 0.0,

    # ── Trailing SL state (per leg — independent) ─────────────────────────────
    # trailing_active_*: True once LTP has fallen through the trigger threshold.
    # trailing_sl_*:     The current trailing SL price; only moves down.
    # Persisted so a crash/restart resumes trailing exactly where it stopped.
    "trailing_active_ce"  : False,
    "trailing_active_pe"  : False,
    "trailing_sl_ce"      : 0.0,
    "trailing_sl_pe"      : 0.0,

    # ── Breakeven SL state (per leg — set after partial exit at a loss) ───────
    # breakeven_active_*: True once the surviving leg's SL has been tightened
    #   to the breakeven price (i.e. the price where total combined P&L = 0).
    # breakeven_sl_*:     The computed breakeven buyback price for the leg.
    # Only activated when closed_pnl < 0 (first leg closed at a loss).
    "breakeven_active_ce" : False,
    "breakeven_active_pe" : False,
    "breakeven_sl_ce"     : 0.0,
    "breakeven_sl_pe"     : 0.0,

    # ── Opening range reference price ─────────────────────────────────────────
    # NIFTY spot captured by job_orb_capture() at ORB_CAPTURE_TIME (09:17 IST).
    # Used by orb_filter_ok() to measure how much NIFTY has moved at entry time.
    # Reset between trades; rewritten fresh each trading day by the ORB job.
    "orb_price"           : 0.0,

    # ── Market context captured at entry ─────────────────────────────────────
    # underlying_ltp: NIFTY spot at the moment of straddle entry.
    #   Also serves as the spot_at_entry reference for spot_move_exit guard.
    # vix_at_entry:   India VIX at entry — baseline for VIX spike monitor.
    # ivr_at_entry:   IV Rank at entry (0–100); logged and Telegram'd.
    # ivp_at_entry:   IV Percentile at entry (0–100); logged and Telegram'd.
    # entry_time:     tz-aware datetime (IST) set at entry; serialised to ISO
    #                 string in JSON, restored to datetime on load.
    # entry_date:     YYYY-MM-DD string; used for stale-state detection on restart
    #                 (if saved_date != today_ist → MIS auto sq-off assumed).
    "underlying_ltp"      : 0.0,
    "vix_at_entry"        : 0.0,
    "ivr_at_entry"        : 0.0,
    "ivp_at_entry"        : 0.0,
    "entry_time"          : None,   # datetime (IST) | None
    "entry_date"          : None,   # "YYYY-MM-DD"   | None

    # ── Margin info captured at entry ─────────────────────────────────────────
    # Stored for logging and Telegram only — not used in trade logic.
    "margin_required"     : 0.0,   # SPAN + Exposure margin from margin guard
    "margin_available"    : 0.0,   # cash + collateral from funds() call

    # ── Running P&L (updated every monitor cycle) ─────────────────────────────
    # today_pnl = closed_pnl + current open leg(s) MTM.
    # Checked against DAILY_PROFIT_TARGET and DAILY_LOSS_LIMIT each monitor tick.
    "today_pnl"           : 0.0,

    # ── Session statistics ─────────────────────────────────────────────────────
    # trade_count: incremented at each entry (0 → 1 on first entry, etc.).
    # exit_reason: human-readable description of HOW the position was closed;
    #   e.g. "CE SL Hit", "Daily Target", "Hard Exit 15:15", "VIX Spike Exit".
    "trade_count"         : 0,
    "exit_reason"         : "",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  Module-level state singleton
#
#  This is the SINGLE live copy of the state dict shared across all modules.
#  All modules must import it by reference, not by value:
#
#    from util.state import state          # ← import the reference (correct)
#    state["ce_active"] = True             # ← mutates the shared object
#
#  NEVER do: local_state = dict(state)    # this creates a disconnected copy
#            local_state["ce_active"] = True   # not visible to other modules
# ═══════════════════════════════════════════════════════════════════════════════

state: dict[str, Any] = dict(INITIAL_STATE)


# ═══════════════════════════════════════════════════════════════════════════════
#  reset_state() — wipe live state back to blank without replacing the object
# ═══════════════════════════════════════════════════════════════════════════════

def reset_state() -> None:
    """
    Reset all state fields to their initial (blank) values.

    Uses dict.update() to mutate the existing object IN PLACE so all modules
    that imported `state` by reference continue to see the reset values.
    Called after a position is fully closed (both legs flat) to prepare for the
    next potential entry.

    WHY NOT `state = dict(INITIAL_STATE)`:
      Rebinding the module-level name creates a NEW dict object. Any other module
      that already did `from util.state import state` would still hold a reference
      to the OLD dict — the reset would be invisible to them.
      Using `state.clear(); state.update(INITIAL_STATE)` guarantees all references
      remain valid.
    """
    state.clear()
    state.update(INITIAL_STATE)


# ═══════════════════════════════════════════════════════════════════════════════
#  _resolve_state_path() — internal helper
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_state_path(state_path: Optional[str | Path]) -> Path:
    """
    Resolve the state file path.
    - If state_path is given, use it (useful for tests with a custom tmp path).
    - If None, fall back to cfg.STATE_FILE from the config singleton.
    - If cfg is also None (config.toml not found), fall back to a local default.
    """
    if state_path is not None:
        return Path(state_path)

    try:
        from util.config_util import cfg  # noqa: PLC0415
        if cfg is not None:
            return Path(cfg.STATE_FILE)
    except ImportError:
        pass

    # Last-resort fallback — should never reach here in production
    return Path("strategy_state.json")


# ═══════════════════════════════════════════════════════════════════════════════
#  save_state() — atomic crash-safe write
# ═══════════════════════════════════════════════════════════════════════════════

def save_state(state_path: Optional[str | Path] = None) -> None:
    """
    Atomically write the current in-memory state dict to a JSON file.

    Write strategy:
        1. Shallow-copy the live state dict.
        2. Serialize `entry_time` (datetime → ISO string) so json.dump() succeeds.
        3. Write to a temp file in the SAME directory as the state file
           (same filesystem → rename is atomic even on network mounts).
        4. os.replace(temp → state_file) — atomic on both POSIX and Windows NTFS.
        5. On any error: clean up the temp file and log a WARNING (never raises).

    WHY atomic write:
        A direct json.dump(path) would leave a partially-written file on crash or
        SIGKILL. The next restart would read a corrupt state, unable to tell whether
        a live position exists — potentially leading to a naked position with no SL.
        The temp-rename pattern guarantees the state file is ALWAYS either the
        previous complete version or the new complete version, never in-between.

    Parameters
    ----------
    state_path : str or Path, optional
        Path to write to. Defaults to cfg.STATE_FILE.

    Notes
    -----
    This function is intentionally non-raising: a state save failure is a WARNING,
    not a CRITICAL. The in-memory state is still authoritative for the current
    session. However, persistent failures should be investigated — on the next
    restart the state cannot be recovered.
    """
    path = _resolve_state_path(state_path)

    try:
        # Shallow copy — we mutate `payload` (entry_time key) without touching
        # the live state dict.
        payload: dict[str, Any] = dict(state)

        # ── Serialize datetime objects to ISO strings for JSON ─────────────────
        # `entry_time` is a tz-aware datetime at runtime but JSON has no datetime
        # type. We serialise to ISO 8601 and restore in load_state().
        if isinstance(payload.get("entry_time"), datetime):
            payload["entry_time"] = payload["entry_time"].isoformat()

        # ── Ensure parent directory exists ────────────────────────────────────
        # Parent is validated at startup by config_util, but the user may have
        # deleted the directory mid-session. mkdir(exist_ok=True) is safe.
        path.parent.mkdir(parents=True, exist_ok=True)

        # ── Atomic write: temp → rename ───────────────────────────────────────
        # mkstemp() creates the temp file in the SAME directory so that the
        # os.replace() rename stays on the same filesystem (required for atomicity).
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".state.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())   # Flush OS buffers → guarantee data on disk

            # os.replace() is atomic on POSIX (rename syscall) and on Windows NTFS.
            os.replace(tmp_path, path)

        except Exception:
            # Clean up the temp file — leave the existing state file intact.
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        debug(f"State saved → {path}")

    except Exception as exc:
        warn(f"State save failed: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
#  load_state() — read + type restoration
# ═══════════════════════════════════════════════════════════════════════════════

def load_state(state_path: Optional[str | Path] = None) -> dict[str, Any]:
    """
    Load the persisted state dict from a JSON file.

    Returns:
        - The loaded dict with entry_time restored to a tz-aware IST datetime,
          if the file exists and is valid JSON.
        - An empty dict {} if the file is missing (fresh start).
        - An empty dict {} if the file is corrupt (logs a warning).

    Callers must merge the loaded dict into the live state themselves:
        saved = load_state()
        if saved:
            state.update(saved)

    WHY return {} instead of INITIAL_STATE on missing/corrupt:
        An empty return lets the caller distinguish "file not found" from
        "file loaded successfully" without checking a boolean flag.
        strategy_core reconcile_on_startup() specifically checks `if saved`
        to decide whether a prior session needs to be restored.

    Parameters
    ----------
    state_path : str or Path, optional
        Path to read from. Defaults to cfg.STATE_FILE.

    Notes
    -----
    entry_time deserialization:
        JSON stores entry_time as an ISO 8601 string. This function parses it
        back to a tz-aware datetime in IST. The reconcile logic in strategy_core
        can then directly compare entry_time against datetime.now(IST).

        If the ISO string has timezone info → converted to IST via astimezone().
        If the ISO string is naive (old format) → localized to IST via IST.localize().
        If parsing fails (malformed) → entry_time is set to now_ist() as a safe
        fallback and a warning is logged.
    """
    path = _resolve_state_path(state_path)

    if not path.exists():
        info(f"No state file at {path} — fresh start")
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            loaded: dict[str, Any] = json.load(f)

    except json.JSONDecodeError as exc:
        warn(f"State file is corrupt (JSON error: {exc}) — starting fresh")
        return {}
    except OSError as exc:
        warn(f"State file read error: {exc} — starting fresh")
        return {}

    # ── Restore entry_time: ISO string → tz-aware IST datetime ───────────────
    raw_entry_time = loaded.get("entry_time")
    if isinstance(raw_entry_time, str):
        try:
            parsed = datetime.fromisoformat(raw_entry_time)
            if parsed.tzinfo is None:
                # Naive datetime (saved before IST-awareness fix) → localize to IST
                loaded["entry_time"] = _IST.localize(parsed)
            else:
                # Tz-aware → convert to IST (handles UTC-stored datetimes from VPS)
                loaded["entry_time"] = parsed.astimezone(_IST)
        except (ValueError, OverflowError) as exc:
            warn(
                f"entry_time in state file could not be parsed ('{raw_entry_time}'): {exc} "
                "— defaulting to current IST time"
            )
            loaded["entry_time"] = datetime.now(_IST)

    info(f"State file loaded: {path}")
    return loaded


# ═══════════════════════════════════════════════════════════════════════════════
#  clear_state_file() — remove the persisted state file
# ═══════════════════════════════════════════════════════════════════════════════

def clear_state_file(state_path: Optional[str | Path] = None) -> None:
    """
    Delete the state file from disk.

    Called ONLY after the position is confirmed FULLY FLAT (both legs closed).
    Never called while a position is open — that would prevent recovery on restart.

    On restart with no state file, reconcile_on_startup() starts fresh (Case A).
    On restart WITH a state file, it attempts to restore the position (Case B/C/D).

    Parameters
    ----------
    state_path : str or Path, optional
        Path to delete. Defaults to cfg.STATE_FILE.

    Notes
    -----
    Non-raising: if the file was already deleted (e.g., by a concurrent process
    or manual operator intervention), the function logs a debug message and
    returns without error.
    """
    path = _resolve_state_path(state_path)

    try:
        if path.exists():
            path.unlink()
            info(f"State file cleared: {path}")
        else:
            debug(f"State file already absent: {path}")
    except OSError as exc:
        warn(f"State file remove failed: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI self-test — print current state dict and exercise save/load/clear cycle
#
#  Usage:
#    python util/state.py                        # uses default cfg.STATE_FILE
#    python util/state.py /path/to/state.json    # custom path
#    python util/state.py --roundtrip            # save → load → clear cycle
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    from datetime import timezone

    # ── Determine state file path ────────────────────────────────────────────
    args = sys.argv[1:]
    do_roundtrip = "--roundtrip" in args
    path_args    = [a for a in args if not a.startswith("--")]
    _state_path  = Path(path_args[0]) if path_args else _resolve_state_path(None)

    print("─" * 72)
    print("  STATE MODULE SELF-TEST")
    print("─" * 72)
    print(f"  State file : {_state_path.resolve()}")
    print()

    # ── Load and display if file exists ──────────────────────────────────────
    if _state_path.exists():
        print("  ── Loaded from disk ──")
        _loaded = load_state(_state_path)
        for k, v in _loaded.items():
            print(f"  {k:<30} = {v!r}")
        print()
    else:
        print("  (No state file found — showing initial state defaults)")
        for k, v in INITIAL_STATE.items():
            print(f"  {k:<30} = {v!r}")
        print()

    # ── Round-trip test (save → load → compare → clear) ──────────────────────
    if do_roundtrip:
        import tempfile as _tmp
        _tmp_file = Path(_tmp.mktemp(suffix=".state_test.json"))
        print(f"  ── Round-trip test → {_tmp_file} ──")

        # Prime state with some recognisable values
        reset_state()
        state["in_position"]    = True
        state["ce_active"]      = True
        state["pe_active"]      = True
        state["symbol_ce"]      = "NIFTY25MAR2623000CE"
        state["symbol_pe"]      = "NIFTY25MAR2623000PE"
        state["entry_price_ce"] = 123.50
        state["entry_price_pe"] = 118.75
        state["entry_time"]     = datetime.now(timezone.utc).astimezone(_IST)
        state["entry_date"]     = datetime.now(_IST).strftime("%Y-%m-%d")
        state["vix_at_entry"]   = 17.42
        state["trade_count"]    = 1

        save_state(_tmp_file)
        print(f"  save_state() → {_tmp_file.name}")

        _back = load_state(_tmp_file)
        _ok   = True

        for _key in ("in_position", "ce_active", "symbol_ce", "entry_price_ce",
                     "entry_price_pe", "entry_date", "vix_at_entry", "trade_count"):
            _expected = state[_key]
            _got      = _back.get(_key)
            _match    = _got == _expected
            _ok       = _ok and _match
            _status   = "✓" if _match else "✗"
            print(f"  {_status} {_key:<30} expected={_expected!r:>20}  got={_got!r}")

        # entry_time special: must be tz-aware IST
        _et = _back.get("entry_time")
        _tz_ok = isinstance(_et, datetime) and _et.tzinfo is not None
        _ok = _ok and _tz_ok
        print(f"  {'✓' if _tz_ok else '✗'} entry_time  tz-aware IST             got={_et!r}")

        clear_state_file(_tmp_file)
        _cleared = not _tmp_file.exists()
        _ok = _ok and _cleared
        print(f"  {'✓' if _cleared else '✗'} clear_state_file() — file removed")

        print()
        if _ok:
            print("  All round-trip assertions passed ✓")
        else:
            print("  SOME ASSERTIONS FAILED ✗  — review output above")
            sys.exit(1)
    else:
        print("  Run with --roundtrip to exercise save → load → clear cycle.")

    print("─" * 72)
