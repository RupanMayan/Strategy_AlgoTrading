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
    "StateManager",
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
    "in_position"         : False,
    "ce_active"           : False,
    "pe_active"           : False,

    # ── Leg symbols ───────────────────────────────────────────────────────────
    "symbol_ce"           : "",
    "symbol_pe"           : "",

    # ── Order IDs ─────────────────────────────────────────────────────────────
    "orderid_ce"          : "",
    "orderid_pe"          : "",

    # ── Entry fill prices — basis of ALL SL calculations ─────────────────────
    "entry_price_ce"      : 0.0,
    "entry_price_pe"      : 0.0,

    # ── Exit fill prices — recorded by close_one_leg() for trade log ──────────
    "exit_price_ce"       : 0.0,
    "exit_price_pe"       : 0.0,

    # ── Realised P&L from legs already closed this session ───────────────────
    "closed_pnl"          : 0.0,

    # ── Trailing SL state (per leg — independent) ─────────────────────────────
    "trailing_active_ce"  : False,
    "trailing_active_pe"  : False,
    "trailing_sl_ce"      : 0.0,
    "trailing_sl_pe"      : 0.0,

    # ── Breakeven SL state (per leg — set after partial exit at a loss) ───────
    "breakeven_active_ce" : False,
    "breakeven_active_pe" : False,
    "breakeven_sl_ce"     : 0.0,
    "breakeven_sl_pe"     : 0.0,

    # ── Opening range reference price ─────────────────────────────────────────
    "orb_price"           : 0.0,

    # ── Market context captured at entry ─────────────────────────────────────
    "underlying_ltp"      : 0.0,
    "vix_at_entry"        : 0.0,
    "ivr_at_entry"        : 0.0,
    "ivp_at_entry"        : 0.0,
    "entry_time"          : None,   # datetime (IST) | None
    "entry_date"          : None,   # "YYYY-MM-DD"   | None

    # ── Margin info captured at entry ─────────────────────────────────────────
    "margin_required"     : 0.0,
    "margin_available"    : 0.0,

    # ── Running P&L (updated every monitor cycle) ─────────────────────────────
    "today_pnl"           : 0.0,

    # ── Enriched trade log context ─────────────────────────────────────────────
    # sl_events tracks each partial close as it happens (list of dicts):
    #   [{"leg": "CE", "trigger": "trailing_sl", "time": "...", "ltp": 45.2,
    #     "entry_px": 100.0, "pnl": 3575.0}]
    "sl_events"           : [],
    # filters_passed records which entry filters were checked (list of strings):
    #   ["vix", "ivr", "ivp", "orb", "margin", "momentum"]
    "filters_passed"      : [],
    # is_reentry flags whether this trade was a re-entry after early close
    "is_reentry"          : False,

    # ── Session statistics ─────────────────────────────────────────────────────
    "trade_count"         : 0,
    "exit_reason"         : "",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  StateManager — encapsulates live state dict and persistence operations
# ═══════════════════════════════════════════════════════════════════════════════

class StateManager:
    """
    Manages the in-memory strategy state dict and its crash-safe JSON persistence.

    The live state dict is a plain dict accessible via the `data` property (and
    also exposed as the module-level `state` variable for backward compatibility).
    All modules share the same dict object — mutations are visible immediately.
    """

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._state: dict[str, Any] = dict(initial or INITIAL_STATE)

    @property
    def data(self) -> dict[str, Any]:
        """The live state dict. Mutate in place; all references stay in sync."""
        return self._state

    def reset(self) -> None:
        """
        Reset all state fields to their initial (blank) values.

        Uses dict.clear() + dict.update() to mutate the existing object IN PLACE
        so all modules that imported `state` by reference continue to see the
        reset values.
        """
        self._state.clear()
        self._state.update(INITIAL_STATE)

    # ═══════════════════════════════════════════════════════════════════════════
    #  Path resolution
    # ═══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _resolve_path(state_path: Optional[str | Path]) -> Path:
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

    # ═══════════════════════════════════════════════════════════════════════════
    #  save() — atomic crash-safe write
    # ═══════════════════════════════════════════════════════════════════════════

    def save(self, state_path: Optional[str | Path] = None) -> None:
        """
        Atomically write the current in-memory state dict to a JSON file.

        Write strategy:
            1. Shallow-copy the live state dict.
            2. Serialize `entry_time` (datetime → ISO string) so json.dump() succeeds.
            3. Write to a temp file in the SAME directory as the state file.
            4. os.replace(temp → state_file) — atomic on both POSIX and Windows NTFS.
            5. On any error: clean up the temp file and log a WARNING (never raises).

        Parameters
        ----------
        state_path : str or Path, optional
            Path to write to. Defaults to cfg.STATE_FILE.
        """
        path = self._resolve_path(state_path)

        try:
            payload: dict[str, Any] = dict(self._state)

            # ── Serialize datetime objects to ISO strings for JSON ─────────────
            if isinstance(payload.get("entry_time"), datetime):
                payload["entry_time"] = payload["entry_time"].isoformat()

            # ── Ensure parent directory exists ────────────────────────────────
            path.parent.mkdir(parents=True, exist_ok=True)

            # ── Atomic write: temp → rename ───────────────────────────────────
            fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".state.tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())

                os.replace(tmp_path, path)

            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            debug(f"State saved → {path}")

        except Exception as exc:
            warn(f"State save failed: {exc}")

    # ═══════════════════════════════════════════════════════════════════════════
    #  load() — read + type restoration
    # ═══════════════════════════════════════════════════════════════════════════

    def load(self, state_path: Optional[str | Path] = None) -> dict[str, Any]:
        """
        Load the persisted state dict from a JSON file.

        Returns:
            - The loaded dict with entry_time restored to a tz-aware IST datetime,
              if the file exists and is valid JSON.
            - An empty dict {} if the file is missing (fresh start).
            - An empty dict {} if the file is corrupt (logs a warning).

        Parameters
        ----------
        state_path : str or Path, optional
            Path to read from. Defaults to cfg.STATE_FILE.
        """
        path = self._resolve_path(state_path)

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

        # ── Restore entry_time: ISO string → tz-aware IST datetime ───────────
        raw_entry_time = loaded.get("entry_time")
        if isinstance(raw_entry_time, str):
            try:
                parsed = datetime.fromisoformat(raw_entry_time)
                if parsed.tzinfo is None:
                    loaded["entry_time"] = _IST.localize(parsed)
                else:
                    loaded["entry_time"] = parsed.astimezone(_IST)
            except (ValueError, OverflowError) as exc:
                warn(
                    f"entry_time in state file could not be parsed ('{raw_entry_time}'): {exc} "
                    "— defaulting to current IST time"
                )
                loaded["entry_time"] = datetime.now(_IST)

        info(f"State file loaded: {path}")
        return loaded

    # ═══════════════════════════════════════════════════════════════════════════
    #  clear_file() — remove the persisted state file
    # ═══════════════════════════════════════════════════════════════════════════

    def clear_file(self, state_path: Optional[str | Path] = None) -> None:
        """
        Delete the state file from disk.

        Called ONLY after the position is confirmed FULLY FLAT (both legs closed).

        Parameters
        ----------
        state_path : str or Path, optional
            Path to delete. Defaults to cfg.STATE_FILE.
        """
        path = self._resolve_path(state_path)

        try:
            if path.exists():
                path.unlink()
                info(f"State file cleared: {path}")
            else:
                debug(f"State file already absent: {path}")
        except OSError as exc:
            warn(f"State file remove failed: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
#  Module-level singleton + backward-compatible API
# ═══════════════════════════════════════════════════════════════════════════════

_state_manager = StateManager()

# The SINGLE live copy of the state dict shared across all modules.
# All modules must import it by reference, not by value:
#   from util.state import state          # ← import the reference (correct)
#   state["ce_active"] = True             # ← mutates the shared object
state: dict[str, Any] = _state_manager.data


def reset_state() -> None:
    """Backward-compatible wrapper — delegates to the singleton."""
    _state_manager.reset()


def save_state(state_path: Optional[str | Path] = None) -> None:
    """Backward-compatible wrapper — delegates to the singleton."""
    _state_manager.save(state_path)


def load_state(state_path: Optional[str | Path] = None) -> dict[str, Any]:
    """Backward-compatible wrapper — delegates to the singleton."""
    return _state_manager.load(state_path)


def clear_state_file(state_path: Optional[str | Path] = None) -> None:
    """Backward-compatible wrapper — delegates to the singleton."""
    _state_manager.clear_file(state_path)


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
    _state_path  = Path(path_args[0]) if path_args else StateManager._resolve_path(None)

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
