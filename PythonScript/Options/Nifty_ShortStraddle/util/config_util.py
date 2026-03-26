"""
util/config_util.py  —  TOML config loader for Nifty Short Straddle (Partial)
═══════════════════════════════════════════════════════════════════════════════
Responsibilities:
  1. Load config.toml using tomllib (Python 3.11+ built-in) or tomli fallback
  2. Override sensitive credentials from environment variables
  3. Validate every field with descriptive error messages — fails fast at startup
  4. Compute derived values (DAILY_PROFIT_TARGET, DAILY_LOSS_LIMIT)
  5. Transform complex fields:
       DTE_ENTRY_TIME_MAP keys : str  → int
       DYNAMIC_SL_SCHEDULE     : list[dict] → list[tuple(str, float)]
       log_max_mb              : int  → bytes
  6. Expose a Config dataclass + a module-level singleton (cfg) for import

Usage:
    # Option A — use the module-level singleton (loaded on import):
    from util.config_util import cfg
    print(cfg.OPENALGO_HOST)

    # Option B — load explicitly (custom path or for testing):
    from util.config_util import load_config
    cfg = load_config("/path/to/config.toml")

    # Option C — use the classmethod factory directly:
    from util.config_util import Config
    cfg = Config.from_toml("/path/to/config.toml")

Environment variable overrides (take precedence over config.toml values):
    OPENALGO_APIKEY      → connection.api_key
    TELEGRAM_BOT_TOKEN   → telegram.bot_token
    TELEGRAM_CHAT_ID     → telegram.chat_id

Config file location:
    Resolved relative to this file's parent directory (one level up from util/).
    Default: <strategy_dir>/config.toml
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import dataclasses
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ── .env loader ──────────────────────────────────────────────────────────────
# Load .env file BEFORE any os.getenv() calls so environment variable
# overrides for secrets (OPENALGO_APIKEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
# are available without requiring the user to export them in the shell.
try:
    from dotenv import load_dotenv
    # Look for .env in the strategy directory (parent of util/)
    _ENV_FILE = Path(__file__).parent.parent / ".env"
    load_dotenv(_ENV_FILE, override=False)  # override=False: shell env takes precedence
except ImportError:
    pass  # python-dotenv is optional — env vars can be set in shell or systemd

# ── TOML parser ───────────────────────────────────────────────────────────────
# tomllib is built-in from Python 3.11+.
# For Python 3.10: pip install tomli
try:
    import tomllib                      # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib         # type: ignore[no-redef]
    except ImportError:
        raise ImportError(
            "TOML parser not found.\n"
            "Python 3.11+ has tomllib built-in.\n"
            "For Python 3.10 run: pip install tomli"
        )

# ── Public exports ────────────────────────────────────────────────────────────
__all__ = ["Config", "load_config", "cfg"]

# ── Default config path ───────────────────────────────────────────────────────
# util/config_util.py lives one level below the strategy directory.
# config.toml sits in the strategy directory (parent of util/).
_STRATEGY_DIR        = Path(__file__).parent.parent
_DEFAULT_CONFIG_PATH = _STRATEGY_DIR / "config.toml"

# ── Valid constants ───────────────────────────────────────────────────────────
_VALID_LOG_LEVELS    = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_VALID_LOG_ROTATIONS = {"daily", "size", "none"}
_VALID_PRODUCTS      = {"MIS", "NRML"}

# DDMMMYY format — e.g. "25MAR26", "01JAN27"
_EXPIRY_RE = re.compile(r"^\d{2}[A-Z]{3}\d{2}$")


# ═══════════════════════════════════════════════════════════════════════════════
#  Config dataclass
#  Field names match the original script's UPPERCASE constants exactly so that
#  strategy_core.py requires zero name changes when importing from this module.
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Config:
    # ── Section 0 — Directory Layout ────────────────────────────────────────
    LOGS_DIR: str       # resolved absolute path to logs directory
    DATA_DIR: str       # resolved absolute path to data directory

    # ── Section 1 — OpenAlgo Connection ──────────────────────────────────────
    OPENALGO_HOST:    str
    OPENALGO_API_KEY: str

    # ── Section 2 — Instrument ────────────────────────────────────────────────
    UNDERLYING:     str
    EXCHANGE:       str
    LOT_SIZE:       int
    NUMBER_OF_LOTS: int
    PRODUCT:        str
    STRIKE_OFFSET:  str

    # ── Section 3 — Timing ───────────────────────────────────────────────────
    ENTRY_TIME:         str
    EXIT_TIME:          str
    MONITOR_INTERVAL_S: int
    USE_DTE_ENTRY_MAP:  bool
    DTE_ENTRY_TIME_MAP: dict             # {int → "HH:MM"} keys converted str → int

    # ── Section 4 — DTE Filter ────────────────────────────────────────────────
    TRADE_DTE: list[int]

    # ── Section 5 — Month Filter ──────────────────────────────────────────────
    SKIP_MONTHS: list[int]

    # ── Section 6 — VIX Filter ───────────────────────────────────────────────
    VIX_FILTER_ENABLED: bool
    VIX_MIN:            float
    VIX_MAX:            float

    # ── Section 6A — IVR / IVP Filter ────────────────────────────────────────
    IVR_FILTER_ENABLED:   bool
    IVR_MIN:              float
    IVP_FILTER_ENABLED:   bool
    IVP_MIN:              float
    IVR_FAIL_OPEN:        bool
    VIX_HISTORY_FILE:     str
    VIX_HISTORY_MIN_ROWS: int
    VIX_UPDATE_TIME:      str

    # ── Section 6B — ORB Filter ───────────────────────────────────────────────
    ORB_FILTER_ENABLED: bool
    ORB_CAPTURE_TIME:   str
    ORB_MAX_MOVE_PCT:   float

    # ── Section 7 — Risk Management ──────────────────────────────────────────
    LEG_SL_PERCENT:              float
    DAILY_PROFIT_TARGET_PER_LOT: float
    DAILY_LOSS_LIMIT_PER_LOT:    float
    DAILY_PROFIT_TARGET:         float   # derived: per_lot × number_of_lots
    DAILY_LOSS_LIMIT:            float   # derived: per_lot × number_of_lots
    NET_PNL_GUARD_MAX_DEFER_MIN: int     # max minutes to defer SL via Net P&L Guard (0 = unlimited)

    # ── Section 7A — Margin Guard ─────────────────────────────────────────────
    MARGIN_GUARD_ENABLED:   bool
    MARGIN_BUFFER:          float
    MARGIN_GUARD_FAIL_OPEN: bool
    ATM_STRIKE_ROUNDING:    int

    # ── Section 7B — VIX Spike Monitor ───────────────────────────────────────
    VIX_SPIKE_MONITOR_ENABLED:  bool
    VIX_SPIKE_THRESHOLD_PCT:    float
    VIX_SPIKE_CHECK_INTERVAL_S: int
    VIX_SPIKE_ABS_FLOOR:        float

    # ── Section 7C — Trailing SL ──────────────────────────────────────────────
    TRAILING_SL_ENABLED: bool
    TRAIL_TRIGGER_PCT:   float
    TRAIL_LOCK_PCT:      float

    # ── Section 7D — Dynamic SL Tightening ───────────────────────────────────
    DYNAMIC_SL_ENABLED:  bool
    DYNAMIC_SL_SCHEDULE: list[tuple[str, float]]  # [("HH:MM", sl_pct), ...]

    # ── Section 7E — Combined Premium Decay Exit ─────────────────────────────
    COMBINED_DECAY_EXIT_ENABLED: bool
    COMBINED_DECAY_TARGET_PCT:   float
    COMBINED_DECAY_DTE_OVERRIDE: dict    # {int → float} DTE → decay target % override

    # ── Section 7F — Winner-Leg Early Booking ────────────────────────────────
    WINNER_LEG_EARLY_EXIT_ENABLED:  bool
    WINNER_LEG_DECAY_THRESHOLD_PCT: float

    # ── Section 7 — DTE-aware SL Override ──────────────────────────────────────
    DTE_SL_OVERRIDE: dict              # {int → float}  DTE → SL% override

    # ── Section 7H-B — Recovery Lock ────────────────────────────────────────────
    RECOVERY_LOCK_ENABLED:          bool
    RECOVERY_LOCK_MIN_RS_PER_LOT:   float   # min recovery per lot before trail activates
    RECOVERY_LOCK_TRAIL_PCT:        float   # exit if recovery retraces this % from peak

    # ── Section 7H-C — Momentum Filter ──────────────────────────────────────────
    MOMENTUM_FILTER_ENABLED:        bool
    MOMENTUM_MAX_DRIFT_PCT:         float   # max intraday NIFTY drift from day open for entry

    # ── Section 7H-D — Asymmetric Leg Booking ──────────────────────────────────
    ASYMMETRIC_BOOKING_ENABLED:     bool
    ASYMMETRIC_WINNER_DECAY_PCT:    float   # book winner when decayed below this %
    ASYMMETRIC_LOSER_INTACT_PCT:    float   # only if other leg is above this % of entry

    # ── Section 7H-E — Combined Profit Trailing ────────────────────────────────
    COMBINED_PROFIT_TRAIL_ENABLED:  bool
    COMBINED_PROFIT_TRAIL_ACTIVATE_PCT: float  # activate after combined decays this %
    COMBINED_PROFIT_TRAIL_PCT:      float   # exit if combined retraces this % from peak

    # ── Section 7G — Breakeven SL ─────────────────────────────────────────────
    BREAKEVEN_AFTER_PARTIAL_ENABLED: bool
    BREAKEVEN_GRACE_PERIOD_MIN:      int     # minutes before breakeven SL arms
    BREAKEVEN_BUFFER_PCT:            float   # % buffer above breakeven price

    # ── Section 7G — Spot-Move Exit ───────────────────────────────────────────
    BREAKEVEN_SPOT_EXIT_ENABLED: bool
    BREAKEVEN_SPOT_MULTIPLIER:   float
    SPOT_CHECK_INTERVAL_S:       int

    # ── Section 7H — Re-entry ────────────────────────────────────────────────
    REENTRY_ENABLED:                  bool
    REENTRY_COOLDOWN_MIN:             int
    REENTRY_MAX_LOSS_PER_LOT:         float   # per-lot Rs. threshold
    REENTRY_MAX_LOSS_FOR_REENTRY:     float   # derived: per_lot × number_of_lots
    REENTRY_MAX_PER_DAY:              int

    # ── Section 8 — Expiry ───────────────────────────────────────────────────
    AUTO_EXPIRY:   bool
    MANUAL_EXPIRY: str

    # ── Section 9 — Strategy Name ─────────────────────────────────────────────
    STRATEGY_NAME: str

    # ── Section 9A — WebSocket Live Feed ─────────────────────────────────────
    WEBSOCKET_ENABLED:           bool
    WEBSOCKET_STALENESS_S:       float  # cache staleness threshold (seconds)
    WEBSOCKET_RECONNECT_MAX_S:   float  # max backoff between reconnects

    # ── Section 10 — Telegram ────────────────────────────────────────────────
    TELEGRAM_ENABLED:   bool
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_CHAT_ID:   str

    # ── Section 11 — State & Log Files ───────────────────────────────────────
    STATE_FILE:                 str
    TRADE_LOG_FILE:             str
    QUOTE_FAIL_ALERT_THRESHOLD: int

    # ── Section 12 — Logger ──────────────────────────────────────────────────
    LOG_LEVEL:        str    # "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL"
    LOG_TO_CONSOLE:   bool
    LOG_TO_FILE:      bool
    LOG_FILE:         str
    LOG_ROTATION:     str    # "daily" | "size" | "none"
    LOG_MAX_BYTES:    int    # converted from log_max_mb × 1024 × 1024
    LOG_BACKUP_COUNT: int
    LOG_FORMAT:       str

    # ═══════════════════════════════════════════════════════════════════════════
    #  Static helpers (used by validation)
    # ═══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def _to_minutes(hhmm: str) -> int:
        """Convert HH:MM string to total minutes since midnight. No validation."""
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    @staticmethod
    def _validate_hhmm(field_name: str, value: str, errors: list[str]) -> bool:
        """
        Validate a HH:MM time string. Appends an error if invalid.
        Returns True if valid so callers can safely call _to_minutes() after.
        """
        try:
            h, m = value.split(":")
            if not (0 <= int(h) <= 23 and 0 <= int(m) <= 59):
                raise ValueError(f"hour/minute out of range: {h}:{m}")
            return True
        except (ValueError, TypeError):
            errors.append(f"{field_name} must be HH:MM (24-hour), got '{value}'")
            return False

    # ═══════════════════════════════════════════════════════════════════════════
    #  _validate() — instance method (called after construction)
    # ═══════════════════════════════════════════════════════════════════════════

    def _validate(self) -> None:
        """
        Validate all Config fields.
        Collects ALL errors and raises a single ValueError so the operator sees
        every problem at once rather than fixing one error at a time.
        """
        errors: list[str] = []

        # ── Section 1 — Connection ────────────────────────────────────────────
        if not self.OPENALGO_HOST:
            errors.append("[connection] host is required")
        if not self.OPENALGO_API_KEY:
            errors.append(
                "[connection] api_key is required — set it in config.toml "
                "or export OPENALGO_APIKEY in the environment"
            )

        # ── Section 2 — Instrument ────────────────────────────────────────────
        if self.LOT_SIZE <= 0:
            errors.append("[instrument] lot_size must be > 0")
        if self.NUMBER_OF_LOTS <= 0:
            errors.append("[instrument] number_of_lots must be > 0")
        if self.PRODUCT not in _VALID_PRODUCTS:
            errors.append(f"[instrument] product must be 'MIS' or 'NRML', got '{self.PRODUCT}'")
        if not self.UNDERLYING:
            errors.append("[instrument] underlying cannot be empty")

        # ── Section 3 — Timing ───────────────────────────────────────────────
        entry_ok = self._validate_hhmm("[timing] entry_time", self.ENTRY_TIME, errors)
        exit_ok  = self._validate_hhmm("[timing] exit_time",  self.EXIT_TIME,  errors)
        # Cross-check: entry must be before exit
        if entry_ok and exit_ok:
            if self._to_minutes(self.ENTRY_TIME) >= self._to_minutes(self.EXIT_TIME):
                errors.append(
                    f"[timing] entry_time ({self.ENTRY_TIME}) must be "
                    f"before exit_time ({self.EXIT_TIME})"
                )
        if self.MONITOR_INTERVAL_S <= 0:
            errors.append("[timing] monitor_interval_s must be > 0")
        if self.MONITOR_INTERVAL_S > 60:
            errors.append(
                f"[timing] monitor_interval_s = {self.MONITOR_INTERVAL_S}s is dangerously high "
                "— SL checks fire at most once per interval; recommended max is 60s"
            )
        for dte_key, dte_time in self.DTE_ENTRY_TIME_MAP.items():
            t_ok = self._validate_hhmm(f"[timing.dte_entry_time_map] key {dte_key}", dte_time, errors)
            # Each DTE entry time must also be before exit_time
            if t_ok and exit_ok:
                if self._to_minutes(dte_time) >= self._to_minutes(self.EXIT_TIME):
                    errors.append(
                        f"[timing.dte_entry_time_map] key {dte_key}: "
                        f"entry time {dte_time} must be before exit_time {self.EXIT_TIME}"
                    )

        # ── Section 4 — DTE Filter ────────────────────────────────────────────
        if not self.TRADE_DTE:
            errors.append("[dte_filter] trade_dte cannot be empty — at least one DTE required")
        if any(d < 0 for d in self.TRADE_DTE):
            errors.append("[dte_filter] trade_dte values must be >= 0")

        # ── Section 5 — Month Filter ──────────────────────────────────────────
        if any(m < 1 or m > 12 for m in self.SKIP_MONTHS):
            errors.append("[month_filter] skip_months values must be 1–12")

        # ── Section 6 — VIX Filter ───────────────────────────────────────────
        if self.VIX_MIN < 0:
            errors.append("[vix_filter] vix_min must be >= 0")
        if self.VIX_MIN >= self.VIX_MAX:
            errors.append("[vix_filter] vix_min must be strictly less than vix_max")

        # ── Section 6A — IVR / IVP ───────────────────────────────────────────
        if not (0 <= self.IVR_MIN <= 100):
            errors.append("[ivr_ivp_filter] ivr_min must be 0–100")
        if not (0 <= self.IVP_MIN <= 100):
            errors.append("[ivr_ivp_filter] ivp_min must be 0–100")
        if self.VIX_HISTORY_MIN_ROWS <= 0:
            errors.append("[ivr_ivp_filter] vix_history_min_rows must be > 0")
        self._validate_hhmm("[ivr_ivp_filter] vix_update_time", self.VIX_UPDATE_TIME, errors)

        # ── Section 6B — ORB ─────────────────────────────────────────────────
        self._validate_hhmm("[orb_filter] capture_time", self.ORB_CAPTURE_TIME, errors)
        if self.ORB_MAX_MOVE_PCT <= 0:
            errors.append("[orb_filter] max_move_pct must be > 0")

        # ── Section 7 — Risk ─────────────────────────────────────────────────
        if self.LEG_SL_PERCENT < 0:
            errors.append("[risk] leg_sl_percent must be >= 0  (0 = disabled)")
        if self.DAILY_PROFIT_TARGET_PER_LOT < 0:
            errors.append("[risk] daily_profit_target_per_lot must be >= 0  (0 = disabled)")
        if self.DAILY_LOSS_LIMIT_PER_LOT > 0:
            errors.append(
                "[risk] daily_loss_limit_per_lot must be <= 0 — "
                "use a negative value e.g. -4000  (0 = disabled)"
            )

        # ── Section 7A — Margin Guard ─────────────────────────────────────────
        if self.MARGIN_BUFFER < 1.0:
            errors.append("[risk.margin_guard] margin_buffer must be >= 1.0  (1.20 = 20% headroom)")
        if self.ATM_STRIKE_ROUNDING <= 0:
            errors.append("[risk.margin_guard] strike_rounding must be > 0")

        # ── Section 7B — VIX Spike Monitor ───────────────────────────────────
        if self.VIX_SPIKE_THRESHOLD_PCT <= 0:
            errors.append("[risk.vix_spike_monitor] threshold_pct must be > 0")
        if self.VIX_SPIKE_CHECK_INTERVAL_S <= 0:
            errors.append("[risk.vix_spike_monitor] check_interval_s must be > 0")
        if self.VIX_SPIKE_ABS_FLOOR < 0:
            errors.append("[risk.vix_spike_monitor] abs_floor must be >= 0")
        if self.VIX_SPIKE_ABS_FLOOR < self.VIX_MIN:
            errors.append(
                f"[risk.vix_spike_monitor] abs_floor ({self.VIX_SPIKE_ABS_FLOOR}) is below "
                f"vix_filter.vix_min ({self.VIX_MIN}) — the floor will never activate because "
                "VIX below vix_min would have blocked entry; raise abs_floor to >= vix_min"
            )
        # VIX spike monitor works without VIX filter — filters.py still fetches
        # VIX at entry for the spike baseline even when VIX filter is disabled.

        # ── Section 7C — Trailing SL ──────────────────────────────────────────
        if not (0 < self.TRAIL_TRIGGER_PCT < 100):
            errors.append("[risk.trailing_sl] trigger_pct must be between 0 and 100 (exclusive)")
        if self.TRAIL_LOCK_PCT <= 0:
            errors.append("[risk.trailing_sl] lock_pct must be > 0")

        # ── Section 7D — Dynamic SL ───────────────────────────────────────────
        for i, (t, pct) in enumerate(self.DYNAMIC_SL_SCHEDULE):
            self._validate_hhmm(f"[risk.dynamic_sl] schedule[{i}].time", t, errors)
            if pct <= 0:
                errors.append(f"[risk.dynamic_sl] schedule[{i}].sl_pct must be > 0")
            if pct > self.LEG_SL_PERCENT and self.LEG_SL_PERCENT > 0:
                errors.append(
                    f"[risk.dynamic_sl] schedule[{i}].sl_pct ({pct}%) is larger than "
                    f"risk.leg_sl_percent ({self.LEG_SL_PERCENT}%) — dynamic schedule "
                    "entries must be tighter (smaller) than the base SL"
                )
        # Verify entries are in descending time order (required for first-match logic)
        times = [entry[0] for entry in self.DYNAMIC_SL_SCHEDULE]
        if times != sorted(times, reverse=True):
            errors.append(
                "[risk.dynamic_sl] schedule must be in DESCENDING time order "
                "(latest time first) — e.g. 14:30, 13:30, 12:00"
            )

        # ── Section 7E — Combined Decay Exit ─────────────────────────────────
        if not (0 < self.COMBINED_DECAY_TARGET_PCT < 100):
            errors.append("[risk.combined_decay_exit] decay_target_pct must be 0–100 (exclusive)")

        # ── Section 7F — Winner Leg Booking ──────────────────────────────────
        if not (0 < self.WINNER_LEG_DECAY_THRESHOLD_PCT < 100):
            errors.append("[risk.winner_leg_booking] decay_threshold_pct must be 0–100 (exclusive)")

        # ── Section 7 — DTE SL Override ─────────────────────────────────────
        for dte_key, sl_pct in self.DTE_SL_OVERRIDE.items():
            if sl_pct <= 0:
                errors.append(
                    f"[risk.dte_sl_override] DTE{dte_key} sl_pct must be > 0, got {sl_pct}"
                )

        # ── Section 7G — Breakeven SL ───────────────────────────────────────
        if self.BREAKEVEN_GRACE_PERIOD_MIN < 0:
            errors.append("[risk.breakeven_sl] grace_period_min must be >= 0")
        if self.BREAKEVEN_BUFFER_PCT < 0:
            errors.append("[risk.breakeven_sl] buffer_pct must be >= 0")

        # ── Section 7G — Spot-Move Exit ───────────────────────────────────────
        if self.BREAKEVEN_SPOT_MULTIPLIER <= 0:
            errors.append("[risk.spot_move_exit] spot_multiplier must be > 0")
        if self.SPOT_CHECK_INTERVAL_S <= 0:
            errors.append("[risk.spot_move_exit] check_interval_s must be > 0")

        # ── Section 7H — Re-entry ──────────────────────────────────────────
        if self.REENTRY_ENABLED:
            if self.REENTRY_COOLDOWN_MIN <= 0:
                errors.append("[risk.reentry] cooldown_min must be > 0")
            if self.REENTRY_MAX_LOSS_PER_LOT <= 0:
                errors.append("[risk.reentry] max_loss_per_lot must be > 0")
            if self.REENTRY_MAX_PER_DAY <= 0:
                errors.append("[risk.reentry] max_reentries_per_day must be > 0")

        # ── Section 8 — Expiry ───────────────────────────────────────────────
        if not self.AUTO_EXPIRY:
            if not self.MANUAL_EXPIRY:
                errors.append(
                    "[expiry] manual_expiry is required when auto_expiry = false "
                    "(format: DDMMMYY uppercase e.g. '25MAR26')"
                )
            elif not _EXPIRY_RE.match(self.MANUAL_EXPIRY):
                errors.append(
                    f"[expiry] manual_expiry '{self.MANUAL_EXPIRY}' has wrong format — "
                    "must be DDMMMYY uppercase e.g. '25MAR26', '01JAN27'"
                )

        # ── Section 9 — Strategy Name ─────────────────────────────────────────
        if not self.STRATEGY_NAME.strip():
            errors.append(
                "[strategy] name cannot be empty — must match the strategy name "
                "registered in the OpenAlgo dashboard"
            )

        # ── Section 10 — Telegram ────────────────────────────────────────────
        if self.TELEGRAM_ENABLED:
            if not self.TELEGRAM_BOT_TOKEN:
                errors.append(
                    "[telegram] bot_token is required when enabled = true — "
                    "set in config.toml or export TELEGRAM_BOT_TOKEN"
                )
            if not self.TELEGRAM_CHAT_ID:
                errors.append(
                    "[telegram] chat_id is required when enabled = true — "
                    "set in config.toml or export TELEGRAM_CHAT_ID"
                )

        # ── Section 11 — Files ───────────────────────────────────────────────
        if not self.STATE_FILE:
            errors.append("[files] state_file path cannot be empty")
        if self.TRADE_LOG_FILE:
            trade_log_dir = Path(self.TRADE_LOG_FILE).parent
            if not trade_log_dir.exists():
                # Attempt auto-create (directories section may have been skipped)
                try:
                    trade_log_dir.mkdir(parents=True, exist_ok=True)
                except OSError:
                    errors.append(
                        f"[files] trade_log_file parent directory does not exist and cannot be created: "
                        f"'{trade_log_dir}' — create the directory or set trade_log_file = '' to disable"
                    )
        if self.QUOTE_FAIL_ALERT_THRESHOLD <= 0:
            errors.append("[files] quote_fail_alert_threshold must be > 0")

        # ── Section 12 — Logger ──────────────────────────────────────────────
        if self.LOG_LEVEL.upper() not in _VALID_LOG_LEVELS:
            errors.append(
                f"[logging] log_level must be one of "
                f"{sorted(_VALID_LOG_LEVELS)}, got '{self.LOG_LEVEL}'"
            )
        if self.LOG_ROTATION.lower() not in _VALID_LOG_ROTATIONS:
            errors.append(
                f"[logging] log_rotation must be 'daily', 'size', or 'none', "
                f"got '{self.LOG_ROTATION}'"
            )
        if self.LOG_TO_FILE:
            if not self.LOG_FILE:
                errors.append("[logging] log_file path is required when log_to_file = true")
            else:
                log_dir = Path(self.LOG_FILE).parent
                if not log_dir.exists():
                    try:
                        log_dir.mkdir(parents=True, exist_ok=True)
                    except OSError:
                        errors.append(
                            f"[logging] log_file parent directory does not exist and cannot be created: "
                            f"'{log_dir}' — create the directory or use a relative path"
                        )
        if self.LOG_MAX_BYTES <= 0:
            errors.append("[logging] log_max_mb must be > 0")
        if self.LOG_BACKUP_COUNT <= 0:
            errors.append("[logging] log_backup_count must be > 0")
        if not self.LOG_TO_CONSOLE and not self.LOG_TO_FILE:
            errors.append(
                "[logging] both log_to_console and log_to_file are false — "
                "all log output will be silently discarded; "
                "enable at least one to preserve strategy logs"
            )

        # ── Raise all errors at once ──────────────────────────────────────────
        if errors:
            bullet_list = "\n".join(f"  • {e}" for e in errors)
            raise ValueError(
                f"\nconfig.toml has {len(errors)} validation error(s):\n{bullet_list}\n"
            )

    # ═══════════════════════════════════════════════════════════════════════════
    #  from_toml() — classmethod factory (primary entry point)
    # ═══════════════════════════════════════════════════════════════════════════

    @classmethod
    def from_toml(cls, config_path: str | Path = _DEFAULT_CONFIG_PATH) -> Config:
        """
        Load, validate and return a Config object from a TOML file.

        Parameters
        ----------
        config_path : str or Path, optional
            Path to the TOML config file.
            Defaults to config.toml in the strategy directory (parent of util/).

        Returns
        -------
        Config
            Frozen dataclass with all validated configuration values.

        Raises
        ------
        FileNotFoundError
            If the config file does not exist at the given path.
        ValueError
            If the TOML has a syntax error, or one or more values fail validation.
        """
        config_path = Path(config_path)

        if not config_path.exists():
            raise FileNotFoundError(
                f"Config file not found: {config_path}\n"
                f"Expected at: {config_path.resolve()}"
            )

        # ── Parse TOML ────────────────────────────────────────────────────────
        try:
            with open(config_path, "rb") as f:
                raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(
                f"config.toml has a TOML syntax error:\n  {exc}\n"
                f"File: {config_path.resolve()}"
            ) from exc

        # ── Extract sections (short aliases for readability) ──────────────────
        dirs   = raw.get("directories",         {})
        conn   = raw.get("connection",          {})
        inst   = raw.get("instrument",          {})
        timing = raw.get("timing",              {})
        dte    = raw.get("dte_filter",          {})
        months = raw.get("month_filter",        {})
        vix    = raw.get("vix_filter",          {})
        ivr    = raw.get("ivr_ivp_filter",      {})
        orb    = raw.get("orb_filter",          {})
        risk   = raw.get("risk",                {})
        mg     = risk.get("margin_guard",       {})
        vsm    = risk.get("vix_spike_monitor",  {})
        tsl    = risk.get("trailing_sl",        {})
        dsl    = risk.get("dynamic_sl",         {})
        cde    = risk.get("combined_decay_exit",{})
        wlb    = risk.get("winner_leg_booking", {})
        besl   = risk.get("breakeven_sl",       {})
        sme    = risk.get("spot_move_exit",     {})
        reen   = risk.get("reentry",            {})
        rcvr   = risk.get("recovery_lock",      {})
        momf   = raw.get("filters", {}).get("momentum", {})
        asym   = risk.get("asymmetric_booking", {})
        cpt    = risk.get("combined_profit_trail", {})
        dte_sl = risk.get("dte_sl_override",    {})
        expiry = raw.get("expiry",              {})
        strat  = raw.get("strategy",            {})
        wscfg  = raw.get("websocket",           {})
        tg     = raw.get("telegram",            {})
        files  = raw.get("files",               {})
        logcfg = raw.get("logging",             {})

        # ── Resolve directory paths relative to the strategy directory ────────
        logs_dir_raw = dirs.get("logs_dir", "logs")
        data_dir_raw = dirs.get("data_dir", "data")
        logs_dir = str((_STRATEGY_DIR / logs_dir_raw).resolve()) if not os.path.isabs(logs_dir_raw) else logs_dir_raw
        data_dir = str((_STRATEGY_DIR / data_dir_raw).resolve()) if not os.path.isabs(data_dir_raw) else data_dir_raw

        # Auto-create directories at load time (before validation checks paths)
        os.makedirs(logs_dir, exist_ok=True)
        os.makedirs(data_dir, exist_ok=True)

        # ── Environment variable overrides ────────────────────────────────────
        api_key   = os.getenv("OPENALGO_APIKEY")    or conn.get("api_key",   "")
        tg_token  = os.getenv("TELEGRAM_BOT_TOKEN") or tg.get("bot_token",   "")
        tg_chatid = os.getenv("TELEGRAM_CHAT_ID")   or tg.get("chat_id",     "")

        # ── Derived values ────────────────────────────────────────────────────
        number_of_lots = int(inst.get("number_of_lots", 1))
        profit_per_lot = float(risk.get("daily_profit_target_per_lot", 0))
        loss_per_lot   = float(risk.get("daily_loss_limit_per_lot",    0))

        # ── DTE entry time map: TOML string keys → int keys ──────────────────
        try:
            dte_map = {int(k): v for k, v in timing.get("dte_entry_time_map", {}).items()}
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"[timing.dte_entry_time_map] keys must be integers (0–6), got error: {exc}"
            ) from exc

        # ── Dynamic SL schedule: list[{time, sl_pct}] → list[(str, float)] ───
        raw_schedule = dsl.get("schedule", [])
        try:
            dsl_schedule = [
                (str(entry["time"]), float(entry["sl_pct"]))
                for entry in raw_schedule
            ]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"[risk.dynamic_sl] schedule entries must have 'time' and 'sl_pct' keys, "
                f"got error: {exc}"
            ) from exc

        # ── DTE SL override map: TOML string keys → int keys ─────────────────
        try:
            dte_sl_map = {int(k): float(v) for k, v in dte_sl.items()}
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"[risk.dte_sl_override] keys must be integers (DTE), "
                f"values must be floats (SL %), got error: {exc}"
            ) from exc

        # ── log_max_mb → bytes ────────────────────────────────────────────────
        log_max_bytes = int(logcfg.get("log_max_mb", 10)) * 1024 * 1024

        # ── Resolve file paths into their respective directories ─────────────
        def _resolve_in(directory: str, filename: str) -> str:
            if not filename:
                return ""
            if os.path.isabs(filename):
                return filename
            return str(Path(directory) / filename)

        resolved_log_file     = _resolve_in(logs_dir, logcfg.get("log_file",     "strategy.log"))
        resolved_state_file   = _resolve_in(data_dir, files.get("state_file",    "strategy_state.json"))
        resolved_trade_log    = _resolve_in(data_dir, files.get("trade_log_file","trades.jsonl"))
        resolved_vix_history  = _resolve_in(data_dir, ivr.get("vix_history_file","vix_history.csv"))

        # ── Build Config ──────────────────────────────────────────────────────
        instance = cls(
            # Section 0 — Directory Layout
            LOGS_DIR = logs_dir,
            DATA_DIR = data_dir,

            # Section 1 — Connection
            OPENALGO_HOST    = conn.get("host",    "http://127.0.0.1:5000"),
            OPENALGO_API_KEY = api_key,

            # Section 2 — Instrument
            UNDERLYING     = inst.get("underlying",    "NIFTY"),
            EXCHANGE       = inst.get("exchange",      "NSE_INDEX"),
            LOT_SIZE       = int(inst.get("lot_size",       65)),
            NUMBER_OF_LOTS = number_of_lots,
            PRODUCT        = inst.get("product",       "MIS"),
            STRIKE_OFFSET  = inst.get("strike_offset", "ATM"),

            # Section 3 — Timing
            ENTRY_TIME         = timing.get("entry_time",         "09:30"),
            EXIT_TIME          = timing.get("exit_time",          "15:15"),
            MONITOR_INTERVAL_S = int(timing.get("monitor_interval_s", 15)),
            USE_DTE_ENTRY_MAP  = bool(timing.get("use_dte_entry_map",  True)),
            DTE_ENTRY_TIME_MAP = dte_map,

            # Section 4 — DTE Filter
            TRADE_DTE = list(dte.get("trade_dte", [0, 1, 2, 3, 4])),

            # Section 5 — Month Filter
            SKIP_MONTHS = list(months.get("skip_months", [])),

            # Section 6 — VIX Filter
            VIX_FILTER_ENABLED = bool(vix.get("enabled", True)),
            VIX_MIN            = float(vix.get("vix_min", 14.0)),
            VIX_MAX            = float(vix.get("vix_max", 28.0)),

            # Section 6A — IVR / IVP Filter
            IVR_FILTER_ENABLED   = bool(ivr.get("ivr_filter_enabled",   True)),
            IVR_MIN              = float(ivr.get("ivr_min",              30.0)),
            IVP_FILTER_ENABLED   = bool(ivr.get("ivp_filter_enabled",   True)),
            IVP_MIN              = float(ivr.get("ivp_min",              40.0)),
            IVR_FAIL_OPEN        = bool(ivr.get("ivr_fail_open",        False)),
            VIX_HISTORY_FILE     = resolved_vix_history,
            VIX_HISTORY_MIN_ROWS = int(ivr.get("vix_history_min_rows", 100)),
            VIX_UPDATE_TIME      = ivr.get("vix_update_time",      "15:30"),

            # Section 6B — ORB Filter
            ORB_FILTER_ENABLED = bool(orb.get("enabled",      True)),
            ORB_CAPTURE_TIME   = orb.get("capture_time",  "09:17"),
            ORB_MAX_MOVE_PCT   = float(orb.get("max_move_pct", 0.5)),

            # Section 7 — Risk Management
            LEG_SL_PERCENT              = float(risk.get("leg_sl_percent",              20.0)),
            DAILY_PROFIT_TARGET_PER_LOT = profit_per_lot,
            DAILY_LOSS_LIMIT_PER_LOT    = loss_per_lot,
            DAILY_PROFIT_TARGET         = profit_per_lot * number_of_lots,  # derived
            DAILY_LOSS_LIMIT            = loss_per_lot   * number_of_lots,  # derived
            NET_PNL_GUARD_MAX_DEFER_MIN = int(risk.get("net_pnl_guard_max_defer_min", 15)),

            # Section 7A — Margin Guard
            MARGIN_GUARD_ENABLED   = bool(mg.get("enabled",         True)),
            MARGIN_BUFFER          = float(mg.get("margin_buffer",   1.20)),
            MARGIN_GUARD_FAIL_OPEN = bool(mg.get("fail_open",        True)),
            ATM_STRIKE_ROUNDING    = int(mg.get("strike_rounding",   50)),

            # Section 7B — VIX Spike Monitor
            VIX_SPIKE_MONITOR_ENABLED  = bool(vsm.get("enabled",          True)),
            VIX_SPIKE_THRESHOLD_PCT    = float(vsm.get("threshold_pct",   15.0)),
            VIX_SPIKE_CHECK_INTERVAL_S = int(vsm.get("check_interval_s",  300)),
            VIX_SPIKE_ABS_FLOOR        = float(vsm.get("abs_floor",        18.0)),

            # Section 7C — Trailing SL
            TRAILING_SL_ENABLED = bool(tsl.get("enabled",     True)),
            TRAIL_TRIGGER_PCT   = float(tsl.get("trigger_pct", 50.0)),
            TRAIL_LOCK_PCT      = float(tsl.get("lock_pct",    30.0)),

            # Section 7D — Dynamic SL Tightening
            DYNAMIC_SL_ENABLED  = bool(dsl.get("enabled", True)),
            DYNAMIC_SL_SCHEDULE = dsl_schedule,

            # Section 7E — Combined Premium Decay Exit
            COMBINED_DECAY_EXIT_ENABLED = bool(cde.get("enabled",          True)),
            COMBINED_DECAY_TARGET_PCT   = float(cde.get("decay_target_pct", 60.0)),
            COMBINED_DECAY_DTE_OVERRIDE = {int(k): float(v) for k, v in cde.get("dte_override", {}).items()},

            # Section 7F — Winner-Leg Early Booking
            WINNER_LEG_EARLY_EXIT_ENABLED  = bool(wlb.get("enabled",             True)),
            WINNER_LEG_DECAY_THRESHOLD_PCT = float(wlb.get("decay_threshold_pct", 30.0)),

            # Section 7 — DTE-aware SL Override
            DTE_SL_OVERRIDE = dte_sl_map,

            # Section 7H-B — Recovery Lock
            RECOVERY_LOCK_ENABLED        = bool(rcvr.get("enabled",              True)),
            RECOVERY_LOCK_MIN_RS_PER_LOT = float(rcvr.get("min_recovery_rs_per_lot", 500)),
            RECOVERY_LOCK_TRAIL_PCT      = float(rcvr.get("trail_pct",           50.0)),

            # Section 7H-C — Momentum Filter
            MOMENTUM_FILTER_ENABLED = bool(momf.get("enabled",       True)),
            MOMENTUM_MAX_DRIFT_PCT  = float(momf.get("max_drift_pct", 0.5)),

            # Section 7H-D — Asymmetric Leg Booking
            ASYMMETRIC_BOOKING_ENABLED  = bool(asym.get("enabled",           True)),
            ASYMMETRIC_WINNER_DECAY_PCT = float(asym.get("winner_decay_pct", 40.0)),
            ASYMMETRIC_LOSER_INTACT_PCT = float(asym.get("loser_intact_pct", 80.0)),

            # Section 7H-E — Combined Profit Trailing
            COMBINED_PROFIT_TRAIL_ENABLED      = bool(cpt.get("enabled",      True)),
            COMBINED_PROFIT_TRAIL_ACTIVATE_PCT = float(cpt.get("activate_pct", 30.0)),
            COMBINED_PROFIT_TRAIL_PCT          = float(cpt.get("trail_pct",    40.0)),

            # Section 7G — Breakeven SL
            BREAKEVEN_AFTER_PARTIAL_ENABLED = bool(besl.get("enabled", True)),
            BREAKEVEN_GRACE_PERIOD_MIN      = int(besl.get("grace_period_min", 5)),
            BREAKEVEN_BUFFER_PCT            = float(besl.get("buffer_pct", 10.0)),

            # Section 7G — Spot-Move Exit
            BREAKEVEN_SPOT_EXIT_ENABLED = bool(sme.get("enabled",         True)),
            BREAKEVEN_SPOT_MULTIPLIER   = float(sme.get("spot_multiplier", 1.0)),
            SPOT_CHECK_INTERVAL_S       = int(sme.get("check_interval_s",  60)),

            # Section 7H — Re-entry
            REENTRY_ENABLED              = bool(reen.get("enabled",               False)),
            REENTRY_COOLDOWN_MIN         = int(reen.get("cooldown_min",            30)),
            REENTRY_MAX_LOSS_PER_LOT     = float(reen.get("max_loss_per_lot",     2000)),
            REENTRY_MAX_LOSS_FOR_REENTRY = float(reen.get("max_loss_per_lot", 2000)) * number_of_lots,  # derived
            REENTRY_MAX_PER_DAY          = int(reen.get("max_reentries_per_day",   1)),

            # Section 8 — Expiry
            AUTO_EXPIRY   = bool(expiry.get("auto_expiry",   True)),
            MANUAL_EXPIRY = expiry.get("manual_expiry", ""),

            # Section 9 — Strategy Name
            STRATEGY_NAME = strat.get("name", "Short Straddle"),

            # Section 9A — WebSocket Live Feed
            WEBSOCKET_ENABLED         = bool(wscfg.get("enabled",               True)),
            WEBSOCKET_STALENESS_S     = float(wscfg.get("staleness_timeout_s",  60)),
            WEBSOCKET_RECONNECT_MAX_S = float(wscfg.get("reconnect_max_delay_s", 30)),

            # Section 10 — Telegram
            TELEGRAM_ENABLED   = bool(tg.get("enabled", True)),
            TELEGRAM_BOT_TOKEN = tg_token,
            TELEGRAM_CHAT_ID   = tg_chatid,

            # Section 11 — State & Log Files
            STATE_FILE                 = resolved_state_file,
            TRADE_LOG_FILE             = resolved_trade_log,
            QUOTE_FAIL_ALERT_THRESHOLD = int(files.get("quote_fail_alert_threshold", 3)),

            # Section 12 — Logger
            LOG_LEVEL        = logcfg.get("log_level",    "INFO").upper(),
            LOG_TO_CONSOLE   = bool(logcfg.get("log_to_console", True)),
            LOG_TO_FILE      = bool(logcfg.get("log_to_file",    True)),
            LOG_FILE         = resolved_log_file,
            LOG_ROTATION     = logcfg.get("log_rotation", "daily").lower(),
            LOG_MAX_BYTES    = log_max_bytes,
            LOG_BACKUP_COUNT = int(logcfg.get("log_backup_count", 30)),
            LOG_FORMAT       = logcfg.get(
                "log_format",
                "%(asctime)s [%(levelname)-8s] %(message)s"
            ),
        )

        # ── Validate all fields ───────────────────────────────────────────────
        instance._validate()

        return instance


# ═══════════════════════════════════════════════════════════════════════════════
#  Backward-compatible module-level function
#  Usage: from util.config_util import load_config
# ═══════════════════════════════════════════════════════════════════════════════

def load_config(config_path: str | Path = _DEFAULT_CONFIG_PATH) -> Config:
    """Backward-compatible wrapper around Config.from_toml()."""
    return Config.from_toml(config_path)


# ═══════════════════════════════════════════════════════════════════════════════
#  Module-level singleton — loaded once on first import
#  Usage: from util.config_util import cfg
# ═══════════════════════════════════════════════════════════════════════════════

def _load_default() -> Config | None:
    """
    Load the default config.toml silently on module import.
    Returns None (with a stderr warning) if the file does not exist,
    so the module can be imported safely even before config.toml is created.
    Validation and syntax errors are always fatal — the script exits immediately.
    """
    try:
        return Config.from_toml(_DEFAULT_CONFIG_PATH)
    except FileNotFoundError:
        print(
            f"[config_util] WARNING: {_DEFAULT_CONFIG_PATH} not found. "
            "Call load_config(path) explicitly before using cfg.",
            file=sys.stderr,
        )
        return None
    except ValueError as exc:
        # Syntax or validation errors at import time are always fatal.
        print(f"[config_util] FATAL: {exc}", file=sys.stderr)
        sys.exit(1)


cfg: Config | None = _load_default()


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI self-test — verify config.toml is valid without running the strategy
#
#  Usage:
#    python util/config_util.py                        # uses default config.toml
#    python util/config_util.py /path/to/config.toml  # custom path
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else _DEFAULT_CONFIG_PATH
    print(f"Loading: {path.resolve()}\n")

    try:
        loaded = Config.from_toml(path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"FAILED:\n{exc}")
        sys.exit(1)

    # ── Print all loaded values, masking sensitive credentials ────────────────
    # dataclasses.asdict() is used here — vars() does not work on frozen dataclasses.
    print("═" * 72)
    print("  CONFIG LOADED SUCCESSFULLY")
    print("═" * 72)

    all_values = dataclasses.asdict(loaded)
    for sensitive in ("OPENALGO_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if all_values.get(sensitive):
            all_values[sensitive] = "***"

    for key, val in all_values.items():
        print(f"  {key:<42} = {val}")

    print("═" * 72)
    print(f"\n  {len(all_values)} variables loaded — all validations passed ✓\n")
