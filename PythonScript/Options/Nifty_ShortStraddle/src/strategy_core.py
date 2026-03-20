"""
src/strategy_core.py  —  StrategyCore orchestrator
═══════════════════════════════════════════════════════════════════════
Top-level production orchestrator for the Nifty Short Straddle (Partial)
strategy.

Instantiates all sub-components, validates configuration, and runs the
APScheduler loop until stopped by Ctrl+C, SIGTERM, or an unhandled crash.

Sub-components (each in its own module):
  VIXManager         — src.vix_manager
  FilterEngine       — src.filters
  MarginGuard        — src.risk
  TrailingSLEngine   — src.risk
  OrderEngine        — src.order_engine
  Monitor            — src.monitor
  StartupReconciler  — src.reconciler
═══════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import os
import signal
import tempfile
from datetime import datetime

from src._shared import (
    VERSION,
    cfg, state, save_state,
    info, warn, error, debug, sep,
    telegram,
    _get_client,
    IST,
    DAY_NAMES, MONTH_NAMES,
    now_ist, qty, parse_hhmm, active_legs, sl_level,
    is_api_success, parse_ist_datetime,
)

from src.vix_manager import VIXManager
from src.filters import FilterEngine
from src.risk import MarginGuard, TrailingSLEngine
from src.order_engine import OrderEngine
from src.monitor import Monitor
from src.reconciler import StartupReconciler
from util.notifier import flush as _flush_telegram


class StrategyCore:
    """
    Production orchestrator for the Nifty Short Straddle (Partial) strategy.

    Instantiates all sub-components, validates configuration, and runs the
    APScheduler loop until stopped by Ctrl+C, SIGTERM, or an unhandled crash.
    """

    def __init__(self) -> None:
        self._vix_manager  = VIXManager()
        self._trailing_sl  = TrailingSLEngine()
        self._order_engine = OrderEngine(trailing_sl=self._trailing_sl)
        self._filter       = FilterEngine(vix_manager=self._vix_manager)
        self._margin_guard = MarginGuard()
        self._monitor      = Monitor(
            order_engine=self._order_engine,
            trailing_sl=self._trailing_sl,
            vix_manager=self._vix_manager,
        )
        self._reconciler   = StartupReconciler(order_engine=self._order_engine)

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """
        Sanity-check all configuration constants at startup.
        Raises ValueError on any fatal misconfiguration — prevents a live trade
        being placed with wrong parameters due to a config typo.
        """
        errors: list[str] = []

        # ── API credentials ───────────────────────────────────────────────────
        if cfg.OPENALGO_API_KEY in ("", "your_openalgo_api_key_here"):
            errors.append("OPENALGO_API_KEY is not set (still placeholder)")

        # ── Lot / quantity ────────────────────────────────────────────────────
        if cfg.LOT_SIZE <= 0:
            errors.append(f"LOT_SIZE must be > 0, got {cfg.LOT_SIZE}")
        if cfg.NUMBER_OF_LOTS <= 0:
            errors.append(f"NUMBER_OF_LOTS must be > 0, got {cfg.NUMBER_OF_LOTS}")

        # ── Risk parameters ───────────────────────────────────────────────────
        if cfg.LEG_SL_PERCENT < 0:
            errors.append(f"LEG_SL_PERCENT must be >= 0, got {cfg.LEG_SL_PERCENT}")
        elif cfg.LEG_SL_PERCENT == 0:
            warn(
                "LEG_SL_PERCENT=0 — per-leg stop-loss DISABLED. "
                "Position runs unhedged until hard exit or daily limit."
            )
        if cfg.DAILY_PROFIT_TARGET_PER_LOT < 0:
            errors.append(
                f"DAILY_PROFIT_TARGET_PER_LOT must be >= 0 (use 0 to disable), "
                f"got {cfg.DAILY_PROFIT_TARGET_PER_LOT}"
            )
        if cfg.DAILY_LOSS_LIMIT_PER_LOT > 0:
            errors.append(
                f"DAILY_LOSS_LIMIT_PER_LOT must be <= 0 (negative = loss, use 0 to disable), "
                f"got {cfg.DAILY_LOSS_LIMIT_PER_LOT}"
            )

        # ── Margin guard ──────────────────────────────────────────────────────
        if cfg.MARGIN_BUFFER < 1.0:
            errors.append(
                f"MARGIN_BUFFER must be >= 1.0 (e.g. 1.20 = 20% headroom), "
                f"got {cfg.MARGIN_BUFFER}"
            )
        if cfg.ATM_STRIKE_ROUNDING <= 0:
            errors.append(f"ATM_STRIKE_ROUNDING must be > 0, got {cfg.ATM_STRIKE_ROUNDING}")

        # ── Timing ────────────────────────────────────────────────────────────
        try:
            eh, em = parse_hhmm(cfg.ENTRY_TIME)
            assert 0 <= eh <= 23 and 0 <= em <= 59
        except Exception:
            errors.append(f"ENTRY_TIME invalid: {cfg.ENTRY_TIME!r}  (expected HH:MM)")
        if cfg.USE_DTE_ENTRY_MAP:
            for dte_key, t in cfg.DTE_ENTRY_TIME_MAP.items():
                try:
                    dh, dm = parse_hhmm(t)
                    assert 0 <= dh <= 23 and 0 <= dm <= 59
                except Exception:
                    errors.append(
                        f"DTE_ENTRY_TIME_MAP[{dte_key}] invalid: {t!r}  (expected HH:MM)"
                    )
        try:
            xh, xm = parse_hhmm(cfg.EXIT_TIME)
            assert 0 <= xh <= 23 and 0 <= xm <= 59
        except Exception:
            errors.append(f"EXIT_TIME invalid: {cfg.EXIT_TIME!r}  (expected HH:MM)")

        # Entry must precede exit
        try:
            _eh, _em = parse_hhmm(cfg.ENTRY_TIME)
            _xh, _xm = parse_hhmm(cfg.EXIT_TIME)
            if (_eh, _em) >= (_xh, _xm):
                errors.append(
                    f"ENTRY_TIME ({cfg.ENTRY_TIME}) must be earlier than "
                    f"EXIT_TIME ({cfg.EXIT_TIME})"
                )
            if cfg.USE_DTE_ENTRY_MAP:
                for dte_key, t in cfg.DTE_ENTRY_TIME_MAP.items():
                    try:
                        _dh, _dm = parse_hhmm(t)
                        if (_dh, _dm) >= (_xh, _xm):
                            errors.append(
                                f"DTE_ENTRY_TIME_MAP[{dte_key}] ({t}) must be earlier than "
                                f"EXIT_TIME ({cfg.EXIT_TIME})"
                            )
                    except Exception:
                        pass
        except Exception:
            pass

        if cfg.MONITOR_INTERVAL_S <= 0:
            errors.append(f"MONITOR_INTERVAL_S must be > 0, got {cfg.MONITOR_INTERVAL_S}")

        # ── DTE filter ────────────────────────────────────────────────────────
        if not cfg.TRADE_DTE:
            errors.append("TRADE_DTE is empty — no trading days configured")
        if any(d < 0 for d in cfg.TRADE_DTE):
            errors.append(f"TRADE_DTE contains negative values: {cfg.TRADE_DTE}")

        # ── VIX filter ────────────────────────────────────────────────────────
        if cfg.VIX_FILTER_ENABLED and cfg.VIX_MIN >= cfg.VIX_MAX:
            errors.append(f"VIX_MIN ({cfg.VIX_MIN}) must be < VIX_MAX ({cfg.VIX_MAX})")

        # ── Expiry ────────────────────────────────────────────────────────────
        if not cfg.AUTO_EXPIRY:
            try:
                manual_dt = datetime.strptime(cfg.MANUAL_EXPIRY, "%d%b%y")
                if manual_dt.weekday() != 1:
                    errors.append(
                        f"MANUAL_EXPIRY {cfg.MANUAL_EXPIRY!r} is a "
                        f"{DAY_NAMES[manual_dt.weekday()]} "
                        f"— NIFTY weekly expiry is on Tuesday"
                    )
            except Exception:
                errors.append(
                    f"MANUAL_EXPIRY format invalid: {cfg.MANUAL_EXPIRY!r}  "
                    f"(expected DDMMMYY e.g. 25MAR26)"
                )

        # ── DTE SL Override ───────────────────────────────────────────────────
        if cfg.DTE_SL_OVERRIDE:
            for dte_key, sl_pct in cfg.DTE_SL_OVERRIDE.items():
                if sl_pct <= 0:
                    errors.append(
                        f"DTE_SL_OVERRIDE[DTE{dte_key}] sl_pct must be > 0, "
                        f"got {sl_pct}"
                    )
                if dte_key not in cfg.TRADE_DTE:
                    warn(
                        f"CONFIG NOTE: DTE_SL_OVERRIDE has DTE{dte_key} but "
                        f"TRADE_DTE does not include {dte_key} — override is harmless but unused"
                    )

        # ── Breakeven SL grace/buffer ────────────────────────────────────────
        if cfg.BREAKEVEN_AFTER_PARTIAL_ENABLED:
            if cfg.BREAKEVEN_GRACE_PERIOD_MIN < 0:
                errors.append(
                    f"BREAKEVEN_GRACE_PERIOD_MIN must be >= 0, "
                    f"got {cfg.BREAKEVEN_GRACE_PERIOD_MIN}"
                )
            if cfg.BREAKEVEN_BUFFER_PCT < 0:
                errors.append(
                    f"BREAKEVEN_BUFFER_PCT must be >= 0, "
                    f"got {cfg.BREAKEVEN_BUFFER_PCT}"
                )
            if cfg.BREAKEVEN_GRACE_PERIOD_MIN == 0 and cfg.BREAKEVEN_BUFFER_PCT == 0:
                warn(
                    "CONFIG WARNING: Both BREAKEVEN_GRACE_PERIOD_MIN=0 and "
                    "BREAKEVEN_BUFFER_PCT=0. Breakeven SL may fire instantly "
                    "after partial close — consider setting grace >= 3 min or "
                    "buffer >= 5%."
                )

        # ── Re-entry ─────────────────────────────────────────────────────────
        if cfg.REENTRY_ENABLED:
            if cfg.REENTRY_COOLDOWN_MIN <= 0:
                errors.append(
                    f"REENTRY_COOLDOWN_MIN must be > 0, got {cfg.REENTRY_COOLDOWN_MIN}"
                )
            if cfg.REENTRY_MAX_LOSS_PER_LOT <= 0:
                errors.append(
                    f"REENTRY_MAX_LOSS_PER_LOT must be > 0, "
                    f"got {cfg.REENTRY_MAX_LOSS_PER_LOT}"
                )
            if cfg.REENTRY_MAX_PER_DAY <= 0:
                errors.append(
                    f"REENTRY_MAX_PER_DAY must be > 0, got {cfg.REENTRY_MAX_PER_DAY}"
                )

        # ── Trailing SL ───────────────────────────────────────────────────────
        if cfg.TRAILING_SL_ENABLED:
            if not (0.0 < cfg.TRAIL_TRIGGER_PCT < 100.0):
                errors.append(
                    f"TRAIL_TRIGGER_PCT must be between 0 and 100 (exclusive), "
                    f"got {cfg.TRAIL_TRIGGER_PCT}"
                )
            if cfg.TRAIL_LOCK_PCT <= 0:
                errors.append(
                    f"TRAIL_LOCK_PCT must be > 0 when TRAILING_SL_ENABLED=True, "
                    f"got {cfg.TRAIL_LOCK_PCT}"
                )
            if cfg.LEG_SL_PERCENT <= 0:
                errors.append(
                    "TRAILING_SL_ENABLED=True requires LEG_SL_PERCENT > 0 — "
                    "trailing SL replaces the fixed SL after trigger"
                )
            if cfg.TRAIL_LOCK_PCT >= cfg.LEG_SL_PERCENT and cfg.LEG_SL_PERCENT > 0:
                warn(
                    f"CONFIG WARNING: TRAIL_LOCK_PCT ({cfg.TRAIL_LOCK_PCT}%) >= "
                    f"LEG_SL_PERCENT ({cfg.LEG_SL_PERCENT}%). "
                    f"Trailing SL will be capped at fixed SL at activation — "
                    f"consider reducing TRAIL_LOCK_PCT."
                )

        # ── IVR / IVP filter ──────────────────────────────────────────────────
        if cfg.IVR_FILTER_ENABLED and not (0.0 <= cfg.IVR_MIN <= 100.0):
            errors.append(f"IVR_MIN must be 0–100, got {cfg.IVR_MIN}")
        if cfg.IVP_FILTER_ENABLED and not (0.0 <= cfg.IVP_MIN <= 100.0):
            errors.append(f"IVP_MIN must be 0–100, got {cfg.IVP_MIN}")
        if (cfg.IVR_FILTER_ENABLED or cfg.IVP_FILTER_ENABLED) and not cfg.VIX_HISTORY_FILE:
            errors.append(
                "VIX_HISTORY_FILE must not be empty when IVR/IVP filter is enabled"
            )
        if cfg.VIX_HISTORY_MIN_ROWS <= 0:
            errors.append(
                f"VIX_HISTORY_MIN_ROWS must be > 0, got {cfg.VIX_HISTORY_MIN_ROWS}"
            )
        try:
            vuh, vum = parse_hhmm(cfg.VIX_UPDATE_TIME)
            assert 0 <= vuh <= 23 and 0 <= vum <= 59
        except Exception:
            errors.append(
                f"VIX_UPDATE_TIME invalid: {cfg.VIX_UPDATE_TIME!r}  (expected HH:MM)"
            )

        # ── VIX spike monitor ─────────────────────────────────────────────────
        if cfg.VIX_SPIKE_MONITOR_ENABLED and cfg.VIX_SPIKE_THRESHOLD_PCT <= 0:
            errors.append(
                f"VIX_SPIKE_THRESHOLD_PCT must be > 0 when VIX_SPIKE_MONITOR_ENABLED=True, "
                f"got {cfg.VIX_SPIKE_THRESHOLD_PCT}"
            )
        if cfg.VIX_SPIKE_CHECK_INTERVAL_S <= 0:
            errors.append(
                f"VIX_SPIKE_CHECK_INTERVAL_S must be > 0, "
                f"got {cfg.VIX_SPIKE_CHECK_INTERVAL_S}"
            )
        if cfg.VIX_SPIKE_ABS_FLOOR < 0:
            errors.append(
                f"VIX_SPIKE_ABS_FLOOR must be >= 0 (0=disabled), "
                f"got {cfg.VIX_SPIKE_ABS_FLOOR}"
            )
        elif cfg.VIX_SPIKE_ABS_FLOOR == 0:
            warn("VIX_SPIKE_ABS_FLOOR=0 — absolute floor check disabled, relative threshold only")
        if (
            cfg.VIX_SPIKE_MONITOR_ENABLED
            and cfg.VIX_SPIKE_CHECK_INTERVAL_S < cfg.MONITOR_INTERVAL_S
        ):
            errors.append(
                f"VIX_SPIKE_CHECK_INTERVAL_S ({cfg.VIX_SPIKE_CHECK_INTERVAL_S}s) must be >= "
                f"MONITOR_INTERVAL_S ({cfg.MONITOR_INTERVAL_S}s) — "
                f"spike check cannot run faster than the monitor loop"
            )

        # ── Dynamic SL schedule ───────────────────────────────────────────────
        if cfg.DYNAMIC_SL_ENABLED:
            if not cfg.DYNAMIC_SL_SCHEDULE:
                errors.append(
                    "DYNAMIC_SL_SCHEDULE is empty — must have at least one entry "
                    "when DYNAMIC_SL_ENABLED=True"
                )
            for i, entry in enumerate(cfg.DYNAMIC_SL_SCHEDULE):
                if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
                    errors.append(
                        f"DYNAMIC_SL_SCHEDULE[{i}] must be a (HH:MM, sl_pct) pair, "
                        f"got {entry!r}"
                    )
                    continue
                t_str, sl_pct = entry
                try:
                    th, tm = parse_hhmm(t_str)
                    assert 0 <= th <= 23 and 0 <= tm <= 59
                except Exception:
                    errors.append(
                        f"DYNAMIC_SL_SCHEDULE[{i}] time {t_str!r} is invalid (expected HH:MM)"
                    )
                if sl_pct <= 0:
                    errors.append(
                        f"DYNAMIC_SL_SCHEDULE[{i}] sl_pct must be > 0, got {sl_pct}"
                    )
                if sl_pct > cfg.LEG_SL_PERCENT:
                    errors.append(
                        f"DYNAMIC_SL_SCHEDULE[{i}] sl_pct ({sl_pct}%) > "
                        f"LEG_SL_PERCENT ({cfg.LEG_SL_PERCENT}%) — "
                        f"scheduled SL must be <= base SL (tightening only)"
                    )
            times = []
            for entry in cfg.DYNAMIC_SL_SCHEDULE:
                if isinstance(entry, (list, tuple)) and len(entry) == 2:
                    try:
                        times.append(entry[0])
                    except Exception:
                        pass
            if times != sorted(times, reverse=True):
                warn(
                    "CONFIG WARNING: DYNAMIC_SL_SCHEDULE entries are not in descending "
                    "time order. First-match logic may give unexpected results — "
                    "sort latest time first."
                )

        # ── Combined decay exit ──────────────────────────────────────────────
        if cfg.COMBINED_DECAY_EXIT_ENABLED:
            if not (0.0 < cfg.COMBINED_DECAY_TARGET_PCT < 100.0):
                errors.append(
                    f"COMBINED_DECAY_TARGET_PCT must be between 0 and 100 (exclusive), "
                    f"got {cfg.COMBINED_DECAY_TARGET_PCT}"
                )

        # ── Winner-leg early booking ─────────────────────────────────────────
        if cfg.WINNER_LEG_EARLY_EXIT_ENABLED:
            if not (0.0 < cfg.WINNER_LEG_DECAY_THRESHOLD_PCT < 100.0):
                errors.append(
                    f"WINNER_LEG_DECAY_THRESHOLD_PCT must be between 0 and 100 (exclusive), "
                    f"got {cfg.WINNER_LEG_DECAY_THRESHOLD_PCT}"
                )

        # ── ORB filter ────────────────────────────────────────────────────────
        if cfg.ORB_FILTER_ENABLED:
            try:
                oh, om = parse_hhmm(cfg.ORB_CAPTURE_TIME)
                assert 0 <= oh <= 23 and 0 <= om <= 59
            except Exception:
                errors.append(
                    f"ORB_CAPTURE_TIME invalid: {cfg.ORB_CAPTURE_TIME!r}  (expected HH:MM)"
                )
            if not (0.0 < cfg.ORB_MAX_MOVE_PCT < 100.0):
                errors.append(
                    f"ORB_MAX_MOVE_PCT must be between 0 and 100 (exclusive), "
                    f"got {cfg.ORB_MAX_MOVE_PCT}"
                )
            try:
                oh2, om2 = parse_hhmm(cfg.ORB_CAPTURE_TIME)
                if cfg.USE_DTE_ENTRY_MAP:
                    earliest = min(
                        (parse_hhmm(t) for t in cfg.DTE_ENTRY_TIME_MAP.values()),
                        default=parse_hhmm(cfg.ENTRY_TIME),
                    )
                else:
                    earliest = parse_hhmm(cfg.ENTRY_TIME)
                if (oh2, om2) >= earliest:
                    errors.append(
                        f"ORB_CAPTURE_TIME ({cfg.ORB_CAPTURE_TIME}) must be earlier than "
                        f"all entry times — ORB capture must precede entry"
                    )
            except Exception:
                pass

        # ── Spot-move / breakeven breach exit ─────────────────────────────────
        if cfg.BREAKEVEN_SPOT_EXIT_ENABLED:
            if cfg.BREAKEVEN_SPOT_MULTIPLIER <= 0:
                errors.append(
                    f"BREAKEVEN_SPOT_MULTIPLIER must be > 0 when "
                    f"BREAKEVEN_SPOT_EXIT_ENABLED=True, "
                    f"got {cfg.BREAKEVEN_SPOT_MULTIPLIER}"
                )
            if cfg.SPOT_CHECK_INTERVAL_S <= 0:
                errors.append(
                    f"SPOT_CHECK_INTERVAL_S must be > 0, got {cfg.SPOT_CHECK_INTERVAL_S}"
                )
            if cfg.SPOT_CHECK_INTERVAL_S < cfg.MONITOR_INTERVAL_S:
                errors.append(
                    f"SPOT_CHECK_INTERVAL_S ({cfg.SPOT_CHECK_INTERVAL_S}s) must be >= "
                    f"MONITOR_INTERVAL_S ({cfg.MONITOR_INTERVAL_S}s) — "
                    f"spot check cannot run faster than the monitor loop"
                )

        # ── Quote connectivity ───────────────────────────────────────────────
        if cfg.QUOTE_FAIL_ALERT_THRESHOLD <= 0:
            errors.append(
                f"QUOTE_FAIL_ALERT_THRESHOLD must be > 0, "
                f"got {cfg.QUOTE_FAIL_ALERT_THRESHOLD}"
            )

        # ── Telegram ──────────────────────────────────────────────────────────
        if cfg.TELEGRAM_ENABLED and (
            not cfg.TELEGRAM_BOT_TOKEN or not cfg.TELEGRAM_CHAT_ID
        ):
            warn(
                "CONFIG: TELEGRAM_ENABLED=True but BOT_TOKEN or CHAT_ID is empty — "
                "alerts will be silent"
            )

        # ── Report ────────────────────────────────────────────────────────────
        if errors:
            error("═" * 68)
            error("CONFIGURATION ERRORS — strategy will NOT start:")
            for e in errors:
                error(f"  ✗  {e}")
            error("═" * 68)
            raise ValueError(
                f"Config validation failed with {len(errors)} error(s). See logs above."
            )

        info("Config validation: all checks passed ✓")

    # ── VIX history startup check ─────────────────────────────────────────────

    def _check_vix_history_on_startup(self) -> None:
        """
        Validate VIX history file at startup and log actionable status.

        Checks: file exists, row count (>= VIX_HISTORY_MIN_ROWS), staleness.
        Does NOT block startup — only logs warnings.
        If the file is missing, attempts auto-bootstrap from NSE.
        """
        if not cfg.IVR_FILTER_ENABLED and not cfg.IVP_FILTER_ENABLED:
            info("IVR/IVP filter disabled — skipping VIX history startup check")
            return

        sep()
        info("VIX HISTORY STARTUP CHECK")

        if not os.path.exists(cfg.VIX_HISTORY_FILE):
            warn(f"  VIX history file NOT FOUND: {os.path.abspath(cfg.VIX_HISTORY_FILE)}")
            info("  Auto-bootstrapping from NSE historical VIX data...")
            success = self._vix_manager.bootstrap_history()
            if not success:
                warn("  Auto-bootstrap FAILED.")
                warn("  IVR/IVP filter will SKIP trades (fail-closed) until file is created.")
                warn("  Manual fix: call check.manual_bootstrap_vix() or run main.py --bootstrap")
                sep()
                return

        rows = self._vix_manager.load_history_raw()
        n    = len(rows)

        if n == 0:
            warn(
                f"  VIX history file EXISTS but has 0 valid rows: {cfg.VIX_HISTORY_FILE}\n"
                f"  Check file format: header must be 'date,vix_close', values must be numeric"
            )
            sep()
            return

        latest_date_str = rows[-1][0]
        latest_vix      = rows[-1][1]

        info(f"  File        : {os.path.abspath(cfg.VIX_HISTORY_FILE)}")
        info(f"  Rows        : {n}  (need >= {cfg.VIX_HISTORY_MIN_ROWS} for full accuracy)")
        info(f"  Latest entry: {latest_date_str}  VIX {latest_vix:.2f}")

        if n < cfg.VIX_HISTORY_MIN_ROWS:
            warn(
                f"  Row count {n} < {cfg.VIX_HISTORY_MIN_ROWS} minimum. "
                f"IVR/IVP accuracy is limited. "
                f"{'Add more history from NSE data.' if n < 50 else 'Growing — will improve over time.'}"
            )

        try:
            from datetime import date as _date
            latest_dt = _date.fromisoformat(latest_date_str)
            today     = now_ist().date()
            days_old  = (today - latest_dt).days
            if days_old > 5:
                warn(
                    f"  ⚠ VIX history is {days_old} calendar days stale "
                    f"(last: {latest_date_str}). "
                    f"The 15:30 auto-update job will fix this today."
                )
            else:
                info(f"  Freshness   : {days_old} calendar day(s) old — OK")
        except (ValueError, TypeError):
            warn(f"  Could not parse latest date: {latest_date_str!r}")

        sep()

    # ── Startup banner ────────────────────────────────────────────────────────

    def _print_banner(self) -> None:
        """Print the startup configuration summary to stdout."""
        dte_str  = ", ".join(f"DTE{d}" for d in sorted(cfg.TRADE_DTE))
        skip_str = (
            ", ".join(MONTH_NAMES[m] for m in sorted(cfg.SKIP_MONTHS))
            if cfg.SKIP_MONTHS else "None"
        )
        guard_str = (
            f"ENABLED  buffer={int((cfg.MARGIN_BUFFER - 1) * 100)}%  "
            f"fail_open={cfg.MARGIN_GUARD_FAIL_OPEN}"
            if cfg.MARGIN_GUARD_ENABLED else "DISABLED"
        )

        # Compute day names for each DTE relative to expiry
        def _dte_to_dayname(dte_val: int, expiry_dt) -> str:
            from datetime import timedelta as _td
            try:
                d, count = expiry_dt, 0
                while count < dte_val:
                    d -= _td(days=1)
                    if d.weekday() < 5:
                        count += 1
                return DAY_NAMES[d.weekday()]
            except Exception:
                return "?"

        expiry_date = self._filter._get_expiry_date_silent()
        day_str = " | ".join(
            f"DTE{d}={_dte_to_dayname(d, expiry_date)}" for d in sorted(cfg.TRADE_DTE)
        )

        print("", flush=True)
        print("=" * 72, flush=True)
        print(f"  NIFTY SHORT STRADDLE  v{VERSION}  —  PARTIAL SQUARE OFF", flush=True)
        print(f"  OpenAlgo + Dhan API  |  Restart-Safe  |  Production Grade", flush=True)
        print("=" * 72, flush=True)
        print(f"  Host             : {cfg.OPENALGO_HOST}", flush=True)
        print(f"  Strategy         : {cfg.STRATEGY_NAME}", flush=True)
        print(f"  Underlying       : {cfg.UNDERLYING}  |  Exchange  : {cfg.EXCHANGE}", flush=True)
        print(
            f"  Lot size         : {cfg.LOT_SIZE}  |  Lots : {cfg.NUMBER_OF_LOTS}  "
            f"|  Qty/leg : {qty()}",
            flush=True,
        )
        print(f"  Strike offset    : {cfg.STRIKE_OFFSET}  |  Product : {cfg.PRODUCT}", flush=True)
        if cfg.USE_DTE_ENTRY_MAP:
            dte_entry_str = "  |  ".join(
                f"DTE{d}={cfg.DTE_ENTRY_TIME_MAP[d]}" for d in sorted(cfg.DTE_ENTRY_TIME_MAP)
            )
            print(f"  Entry (DTE-map)  : {dte_entry_str}  |  Hard exit : {cfg.EXIT_TIME} IST", flush=True)
        else:
            print(f"  Entry            : {cfg.ENTRY_TIME} IST  |  Hard exit : {cfg.EXIT_TIME} IST", flush=True)
        print(f"  Monitor interval : every {cfg.MONITOR_INTERVAL_S}s", flush=True)
        print(f"  DTE filter       : {dte_str}  ({day_str})", flush=True)
        print(f"  Skip months      : {skip_str}", flush=True)
        print(f"  VIX filter       : {cfg.VIX_MIN}–{cfg.VIX_MAX}  (enabled: {cfg.VIX_FILTER_ENABLED})", flush=True)

        ivr_str = f"IVR>={cfg.IVR_MIN}" if cfg.IVR_FILTER_ENABLED else "IVR=disabled"
        ivp_str = f"IVP>={cfg.IVP_MIN}%" if cfg.IVP_FILTER_ENABLED else "IVP=disabled"
        print(
            f"  IVR/IVP filter   : {ivr_str}  |  {ivp_str}  |  "
            f"fail_open={cfg.IVR_FAIL_OPEN}  |  history={cfg.VIX_HISTORY_FILE}",
            flush=True,
        )

        spike_str = (
            f"ENABLED  threshold={cfg.VIX_SPIKE_THRESHOLD_PCT}%  "
            f"floor={cfg.VIX_SPIKE_ABS_FLOOR}  "
            f"check_every={cfg.VIX_SPIKE_CHECK_INTERVAL_S}s"
            if cfg.VIX_SPIKE_MONITOR_ENABLED else "DISABLED"
        )
        print(f"  VIX spike monitor: {spike_str}", flush=True)

        trail_str = (
            f"ENABLED  trigger={cfg.TRAIL_TRIGGER_PCT}% of entry  "
            f"lock={cfg.TRAIL_LOCK_PCT}% above LTP"
            if cfg.TRAILING_SL_ENABLED else "DISABLED"
        )
        print(f"  Trailing SL      : {trail_str}", flush=True)

        if cfg.TRAILING_SL_ENABLED:
            sqoff_detail = (
                f"each leg has independent {cfg.LEG_SL_PERCENT}% fixed SL "
                f"→ trailing once {cfg.TRAIL_TRIGGER_PCT}% decayed"
            )
        else:
            sqoff_detail = (
                f"each leg has independent {cfg.LEG_SL_PERCENT}% fixed SL (no trailing)"
            )
        print(f"  Sq-off mode      : PARTIAL — {sqoff_detail}", flush=True)
        print(
            f"  Daily target     : Rs.{cfg.DAILY_PROFIT_TARGET_PER_LOT}/lot × "
            f"{cfg.NUMBER_OF_LOTS} lot(s) = Rs.{cfg.DAILY_PROFIT_TARGET}  (0=disabled)",
            flush=True,
        )
        print(
            f"  Daily limit      : Rs.{cfg.DAILY_LOSS_LIMIT_PER_LOT}/lot × "
            f"{cfg.NUMBER_OF_LOTS} lot(s) = Rs.{cfg.DAILY_LOSS_LIMIT}   (0=disabled)",
            flush=True,
        )
        print(f"  Margin guard     : {guard_str}", flush=True)
        print(
            f"  Auto expiry      : {cfg.AUTO_EXPIRY}  |  "
            f"Manual : {cfg.MANUAL_EXPIRY}  (NIFTY expires TUESDAY)",
            flush=True,
        )
        print(f"  State file       : {os.path.abspath(cfg.STATE_FILE)}", flush=True)
        trade_log_str = os.path.abspath(cfg.TRADE_LOG_FILE) if cfg.TRADE_LOG_FILE else "DISABLED"
        print(f"  Trade log        : {trade_log_str}", flush=True)
        print(
            f"  Quote fail alert : after {cfg.QUOTE_FAIL_ALERT_THRESHOLD} consecutive "
            f"failed ticks (~{cfg.QUOTE_FAIL_ALERT_THRESHOLD * cfg.MONITOR_INTERVAL_S}s)",
            flush=True,
        )
        print(f"  Telegram         : {cfg.TELEGRAM_ENABLED}", flush=True)
        print("=" * 72, flush=True)
        print(
            f"  Backtest (2019-2026): P&L Rs.5,04,192 | Win 66.71% | MaxDD Rs.34,179",
            flush=True,
        )
        print(f"  Avg/trade Rs.289 | Return/MDD 1.38 | 1746 trades", flush=True)
        print("─" * 72, flush=True)
        print(f"  Partial logic : each leg has its OWN {cfg.LEG_SL_PERCENT}% SL.", flush=True)
        print(f"  SL on CE → close CE only. PE continues with its own SL.", flush=True)
        print(f"  SL on PE → close PE only. CE continues with its own SL.", flush=True)
        print(f"  combined P&L  = closed_pnl + open_leg_mtm", flush=True)
        print(f"  DTE method    : TRADING days (AlgoTest-compatible, weekends excluded).", flush=True)
        print(f"  DTE filter    : trades ONLY on {dte_str} of weekly expiry cycle.", flush=True)
        print(f"  Margin guard  : funds() + margin() checked before every entry.", flush=True)
        if cfg.DTE_SL_OVERRIDE:
            dte_sl_str = "  |  ".join(
                f"DTE{d}={sl:.0f}%" for d, sl in sorted(cfg.DTE_SL_OVERRIDE.items())
            )
            print(f"  DTE SL override: {dte_sl_str}  (others use {cfg.LEG_SL_PERCENT}%)", flush=True)
        if cfg.BREAKEVEN_AFTER_PARTIAL_ENABLED:
            print(
                f"  Breakeven SL   : grace={cfg.BREAKEVEN_GRACE_PERIOD_MIN}min  "
                f"buffer={cfg.BREAKEVEN_BUFFER_PCT}%",
                flush=True,
            )
        if cfg.REENTRY_ENABLED:
            print(
                f"  Re-entry       : ENABLED  cooldown={cfg.REENTRY_COOLDOWN_MIN}min  "
                f"max_loss=Rs.{cfg.REENTRY_MAX_LOSS_PER_LOT:.0f}/lot "
                f"(Rs.{cfg.REENTRY_MAX_LOSS_FOR_REENTRY:.0f} total)  "
                f"max/day={cfg.REENTRY_MAX_PER_DAY}",
                flush=True,
            )
        print(f"  Analyze Mode  : paper/live is set in the OpenAlgo dashboard.", flush=True)
        print("=" * 72, flush=True)
        print("", flush=True)

    # ── Manual controls ───────────────────────────────────────────────────────

    def check_connection(self) -> None:
        """Test OpenAlgo connection and display account funds + collateral."""
        sep()
        info("Testing OpenAlgo connection...")
        try:
            client = _get_client()
            resp = client.funds()
            if is_api_success(resp):
                data = resp.get("data", {})
                cash = float(data.get("availablecash",  0) or 0)
                coll = float(data.get("collateral",     0) or 0)
                used = float(data.get("utiliseddebits", 0) or 0)
                m2m  = float(data.get("m2munrealized",  0) or 0)
                info("Connection       : OK")
                info(f"Available cash   : Rs.{cash:,.2f}")
                info(f"Collateral       : Rs.{coll:,.2f}")
                info(f"Total available  : Rs.{cash + coll:,.2f}")
                info(f"Utilised debits  : Rs.{used:,.2f}")
                info(f"M2M Unrealised   : Rs.{m2m:,.2f}")
            else:
                error(f"Connection FAILED: {resp}")
        except Exception as exc:
            error(f"Connection exception: {exc}")
        sep()

    def manual_entry(self) -> None:
        """Force entry now — still runs all filters inside job_entry()."""
        info("MANUAL ENTRY triggered")
        self._job_entry()

    def manual_exit(self) -> None:
        """Force close all active legs immediately."""
        info("MANUAL EXIT triggered")
        self._order_engine.close_all(reason="Manual Exit by Operator")

    def show_state(self) -> None:
        """Print full in-memory state + computed SL levels + DTE info to stdout."""
        sep()
        info("STATE DUMP:")
        for k, v in state.items():
            print(f"    {k:<24} : {v}", flush=True)
        print(f"    {'sl_ce (computed)':<24} : Rs.{sl_level('CE'):.2f}", flush=True)
        print(f"    {'sl_pe (computed)':<24} : Rs.{sl_level('PE'):.2f}", flush=True)
        print(f"    {'active_legs':<24} : {active_legs()}", flush=True)

        dte        = self._filter.get_dte()
        weekday    = now_ist().date().weekday()
        is_weekend = weekday >= 5

        print(f"    {'current_dte':<24} : DTE{dte} ({DAY_NAMES[weekday]})", flush=True)
        if is_weekend:
            print(
                f"    {'dte_in_filter':<24} : False  "
                f"(weekend — blocked by weekend guard regardless of DTE)",
                flush=True,
            )
        else:
            print(
                f"    {'dte_in_filter':<24} : {dte in cfg.TRADE_DTE}  "
                f"(TRADE_DTE={['DTE' + str(d) for d in sorted(cfg.TRADE_DTE)]})",
                flush=True,
            )
        sep()

    def check_margin_now(self) -> None:
        """Manual margin check — test without placing a trade."""
        expiry = self._filter.get_expiry()
        result = self._margin_guard.check(expiry)
        info(f"Margin check result: {'PASS ✓' if result else 'FAIL ✗'}")

    def manual_bootstrap_vix(self) -> None:
        """Force re-bootstrap of vix_history.csv from NSE."""
        info("MANUAL VIX HISTORY BOOTSTRAP triggered")
        success = self._vix_manager.bootstrap_history()
        if success:
            rows = self._vix_manager.load_history_raw()
            info(f"vix_history.csv ready: {len(rows)} rows")
        else:
            warn("Bootstrap failed — check NSE connectivity and try again")

    # ── Scheduled jobs ────────────────────────────────────────────────────────

    def _job_orb_capture(self) -> None:
        """
        Opening Range capture job — fires once at ORB_CAPTURE_TIME (default 09:17).

        Fetches live NIFTY spot and stores it as state["orb_price"].
        orb_filter_ok() compares the entry-time spot to this reference.
        Fails-open: 0.0 stored on failure, filter is bypassed at entry time.
        """
        if not cfg.ORB_FILTER_ENABLED:
            debug("ORB capture job: ORB_FILTER_ENABLED=False — skipping")
            return
        if now_ist().weekday() >= 5:
            debug("ORB capture job: weekend — skipping")
            return

        sep()
        info(f"ORB CAPTURE | {now_ist().strftime('%H:%M:%S IST')} — fetching NIFTY opening reference")

        spot = self._monitor._fetch_spot_ltp()
        if spot > 0:
            state["orb_price"] = spot
            save_state()
            info(f"  ORB reference captured: NIFTY Rs.{spot:.2f}")
            info(f"  Entry-time filter: skip if NIFTY moves >{cfg.ORB_MAX_MOVE_PCT}% from this level")
        else:
            state["orb_price"] = 0.0
            warn(
                "  ORB capture FAILED — NIFTY LTP unavailable. "
                "ORB filter will be bypassed at entry time (fail-open)."
            )
            telegram(
                f"⚠️ ORB Capture FAILED at {cfg.ORB_CAPTURE_TIME}\n"
                f"NIFTY spot unavailable — ORB filter bypassed today.\n"
                f"Check OpenAlgo / NSE connectivity."
            )
        sep()

    def _job_entry(self) -> None:
        """
        Entry job — fires at ENTRY_TIME (or DTE-map times) on weekdays.

        Filter chain (short-circuits on first failure):
          0. DTE-aware entry time guard (USE_DTE_ENTRY_MAP mode)
          1. Duplicate guard (already in position)
          2. DTE filter + month filter
          3. VIX filter
          4. IVR / IVP filter (reuses VIX from step 3)
          5. Opening range filter (ORB)
          6. Margin guard
          7. Reset daily counters + place_entry()

        FIX-A (v5.1.0): expiry resolved ONCE and passed to both margin check
        and place_entry() — eliminates theoretical race at Tuesday 15:30.
        """
        sep()
        info(f"ENTRY JOB | {now_ist().strftime('%A %d-%b-%Y %H:%M:%S IST')}")
        sep()

        # ── 0. DTE-aware entry time guard ─────────────────────────────────────
        dte_now = None
        if cfg.USE_DTE_ENTRY_MAP:
            now_hhmm        = now_ist().strftime("%H:%M")
            dte_now         = self._filter.get_dte()
            effective_entry = cfg.DTE_ENTRY_TIME_MAP.get(dte_now, cfg.ENTRY_TIME)
            if now_hhmm != effective_entry:
                info(
                    f"DTE{dte_now} effective entry is {effective_entry} — "
                    f"current time {now_hhmm} does not match; this job slot skipped"
                )
                return

        # ── 1. Duplicate guard ─────────────────────────────────────────────────
        if state["in_position"]:
            warn("Already in position — entry skipped (duplicate guard)")
            return

        # ── 1a. Re-entry guard — check if this is a valid re-entry attempt ───
        today_str = now_ist().strftime("%Y-%m-%d")
        trade_count_today = state.get("trade_count", 0)
        reentry_count     = state.get("reentry_count_today", 0)
        last_close_time   = state.get("last_close_time")
        last_trade_pnl    = state.get("last_trade_pnl", 0.0)
        last_entry_date   = state.get("entry_date")

        # Reset reentry counter on a new day
        if last_entry_date and last_entry_date != today_str:
            state["reentry_count_today"] = 0
            reentry_count = 0

        # Determine if this is the first trade or a re-entry
        is_first_trade = (last_close_time is None) or (last_entry_date != today_str)

        if not is_first_trade:
            # This is a potential re-entry — check re-entry conditions
            if not cfg.REENTRY_ENABLED:
                info("Re-entry disabled — only one trade per day allowed")
                return

            if reentry_count >= cfg.REENTRY_MAX_PER_DAY:
                info(
                    f"Re-entry limit reached: {reentry_count}/{cfg.REENTRY_MAX_PER_DAY} "
                    f"re-entries today — no more entries"
                )
                return

            # Check cooldown
            if last_close_time:
                close_dt = parse_ist_datetime(last_close_time)
                if close_dt:
                    elapsed_min = (now_ist() - close_dt).total_seconds() / 60.0
                    if elapsed_min < cfg.REENTRY_COOLDOWN_MIN:
                        info(
                            f"Re-entry cooldown: {elapsed_min:.0f}m elapsed < "
                            f"{cfg.REENTRY_COOLDOWN_MIN}m required — skipping"
                        )
                        return

            # Check previous trade loss threshold
            if last_trade_pnl < 0 and abs(last_trade_pnl) > cfg.REENTRY_MAX_LOSS_FOR_REENTRY:
                info(
                    f"Re-entry blocked: previous loss Rs.{last_trade_pnl:.0f} "
                    f"exceeds max Rs.{cfg.REENTRY_MAX_LOSS_FOR_REENTRY:.0f} "
                    f"({cfg.REENTRY_MAX_LOSS_PER_LOT:.0f}/lot × {cfg.NUMBER_OF_LOTS})"
                )
                return

            info(
                f"RE-ENTRY ATTEMPT #{reentry_count + 1} "
                f"(prev P&L Rs.{last_trade_pnl:.0f}, "
                f"cooldown OK) — running full filter chain"
            )

        # ── Track which filters pass for enriched trade log ─────────────────────
        filters_passed: list[str] = []

        # ── 2. DTE filter + month filter ───────────────────────────────────────
        if not self._filter.dte_filter_ok(dte_now):
            return
        filters_passed.append("dte")

        # ── 3. VIX filter ──────────────────────────────────────────────────────
        if not self._filter.vix_ok():
            return
        filters_passed.append("vix")

        # ── 4. IVR / IVP filter — reuses VIX stored in state by vix_ok() ──────
        if not self._vix_manager.ivr_ivp_ok(state["vix_at_entry"]):
            return
        filters_passed.append("ivr_ivp")

        # ── 5. Opening range filter ────────────────────────────────────────────
        if not self._filter.orb_filter_ok():
            return
        filters_passed.append("orb")

        # ── 5B. FIX-XXVI: Momentum filter (re-entry only) ────────────────────
        if not is_first_trade:
            if not self._filter.momentum_filter_ok():
                return
            filters_passed.append("momentum")

        # ── 6. Resolve expiry ONCE (FIX-A) and run margin guard ───────────────
        expiry = self._filter.get_expiry()
        if not self._margin_guard.check(expiry):
            error("Entry ABORTED — insufficient margin (cash + collateral)")
            return
        filters_passed.append("margin")

        # ── 7. Reset trade-level counters and place entry ──────────────────────
        # FIX-XVII: On re-entry, carry forward cumulative daily P&L so the daily
        # loss limit sees the TOTAL day's losses, not just the current trade.
        # First trade of the day starts from zero; re-entries carry forward.
        if is_first_trade:
            state["cumulative_daily_pnl"] = 0.0
            state["today_pnl"]  = 0.0
            state["closed_pnl"] = 0.0
        else:
            prev_pnl = state.get("last_trade_pnl", 0.0)
            cumulative = state.get("cumulative_daily_pnl", 0.0) + prev_pnl
            state["cumulative_daily_pnl"] = cumulative
            state["today_pnl"]  = cumulative
            state["closed_pnl"] = cumulative
            info(
                f"  Cumulative daily P&L carried forward: Rs.{cumulative:.0f} "
                f"(prev trade: Rs.{prev_pnl:.0f})"
            )

        # Store current DTE for DTE-aware SL override (used by _base_sl_percent())
        if dte_now is None:
            dte_now = self._filter.get_dte()
        state["current_dte"] = dte_now

        success = self._order_engine.place_entry(expiry)
        if success:
            # Store enriched trade log context AFTER place_entry() — place_entry()
            # resets sl_events/filters_passed to [], so we set them after.
            state["filters_passed"] = filters_passed
            state["is_reentry"]     = not is_first_trade
            save_state()
            if not is_first_trade:
                # Track re-entry count
                state["reentry_count_today"] = reentry_count + 1
                info(f"Re-entry #{state['reentry_count_today']} placed successfully")
        if not success:
            error("Entry FAILED — no position opened today")
            telegram("Entry FAILED — no position opened. Check logs.")

    def _job_exit(self) -> None:
        """Hard exit — fires at EXIT_TIME, closes ALL remaining active legs."""
        sep()
        info(f"EXIT JOB | {now_ist().strftime('%A %d-%b-%Y %H:%M:%S IST')}")
        sep()

        if not state["in_position"]:
            info("No open position at scheduled exit — nothing to do")
            return

        active = active_legs()
        info(f"Hard exit — active legs: {active}")
        self._order_engine.close_all(reason=f"Scheduled Hard Exit at {cfg.EXIT_TIME}")

    def _job_monitor(self) -> None:
        """Monitor tick — fires every MONITOR_INTERVAL_S seconds."""
        if state["in_position"]:
            self._monitor.monitor_pnl()

    def _job_update_vix_history(self) -> None:
        """
        Daily VIX history maintenance — fires once at VIX_UPDATE_TIME (15:30 IST).

        Appends today's closing VIX to vix_history.csv for tomorrow's IVR/IVP
        filter.  Duplicate-safe (idempotent), atomic write (temp + rename).
        """
        now_dt = now_ist()
        if now_dt.weekday() >= 5:
            debug("VIX history update: weekend — skipping")
            return

        today_str = now_dt.date().isoformat()

        rows = self._vix_manager.load_history_raw()
        if rows and rows[-1][0] == today_str:
            debug(
                f"VIX history: {today_str} already recorded "
                f"(VIX {rows[-1][1]:.2f}) — no update needed"
            )
            return

        vix = self._vix_manager.fetch_vix()
        if vix <= 0:
            warn(f"VIX history update: VIX unavailable for {today_str} — skipping")
            telegram(
                f"⚠️ VIX history: daily update FAILED for {today_str}\n"
                f"IVR/IVP data will be 1 day stale tomorrow.\n"
                f"Check OpenAlgo / NSE connectivity."
            )
            return

        rows.append((today_str, vix))
        if len(rows) > 300:
            rows = rows[-300:]

        try:
            hist_dir = os.path.dirname(os.path.abspath(cfg.VIX_HISTORY_FILE)) or "."
            fd, tmp_path = tempfile.mkstemp(dir=hist_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    f.write("date,vix_close\n")
                    for d, v in rows:
                        f.write(f"{d},{v:.2f}\n")
                os.replace(tmp_path, cfg.VIX_HISTORY_FILE)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            info(
                f"VIX history updated: {today_str} → VIX {vix:.2f}  "
                f"({len(rows)} rows in {cfg.VIX_HISTORY_FILE})"
            )
        except Exception as exc:
            warn(f"VIX history write failed: {exc}")
            telegram(f"⚠️ VIX history write FAILED: {exc}")

    # ── Production run ────────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Production startup:
          1. Validate configuration (raises on misconfiguration)
          2. Print banner
          3. Test connection + show funds
          4. Reconcile state with broker
          5. Check VIX history file (auto-bootstrap if missing)
          6. Start APScheduler (entry / exit / monitor / vix-update / orb)
          7. Graceful shutdown on Ctrl+C, SIGTERM, or crash
        """
        from apscheduler.schedulers.blocking import BlockingScheduler

        self._validate_config()
        self._print_banner()
        self.check_connection()
        self._reconciler.reconcile()
        self._check_vix_history_on_startup()

        exit_h,    exit_m    = parse_hhmm(cfg.EXIT_TIME)
        vix_upd_h, vix_upd_m = parse_hhmm(cfg.VIX_UPDATE_TIME)
        orb_h,     orb_m     = parse_hhmm(cfg.ORB_CAPTURE_TIME)

        scheduler = BlockingScheduler(timezone=IST)

        # ── Entry job(s) ───────────────────────────────────────────────────────
        if cfg.USE_DTE_ENTRY_MAP:
            unique_times = sorted(set(cfg.DTE_ENTRY_TIME_MAP.values()))
            for t in unique_times:
                th, tm = parse_hhmm(t)
                scheduler.add_job(
                    func               = self._job_entry,
                    trigger            = "cron",
                    day_of_week        = "mon-fri",
                    hour               = th,
                    minute             = tm,
                    id                 = f"entry_job_{t.replace(':', '')}",
                    name               = f"Entry {t} (DTE-map)",
                    misfire_grace_time = 60,
                )
            info(f"DTE-aware entry scheduled at: {', '.join(unique_times)} IST")
        else:
            entry_h, entry_m = parse_hhmm(cfg.ENTRY_TIME)
            scheduler.add_job(
                func               = self._job_entry,
                trigger            = "cron",
                day_of_week        = "mon-fri",
                hour               = entry_h,
                minute             = entry_m,
                id                 = "entry_job",
                name               = f"Entry {cfg.ENTRY_TIME}",
                misfire_grace_time = 60,
            )

        scheduler.add_job(
            func               = self._job_exit,
            trigger            = "cron",
            day_of_week        = "mon-fri",
            hour               = exit_h,
            minute             = exit_m,
            id                 = "exit_job",
            name               = f"Exit {cfg.EXIT_TIME}",
            misfire_grace_time = 120,
        )
        scheduler.add_job(
            func    = self._job_monitor,
            trigger = "interval",
            seconds = cfg.MONITOR_INTERVAL_S,
            id      = "monitor_job",
            name    = f"Monitor {cfg.MONITOR_INTERVAL_S}s",
        )
        if cfg.ORB_FILTER_ENABLED:
            scheduler.add_job(
                func               = self._job_orb_capture,
                trigger            = "cron",
                day_of_week        = "mon-fri",
                hour               = orb_h,
                minute             = orb_m,
                id                 = "orb_capture_job",
                name               = f"ORB Capture {cfg.ORB_CAPTURE_TIME}",
                misfire_grace_time = 120,
            )
            info(f"ORB capture scheduled at {cfg.ORB_CAPTURE_TIME} IST (mon-fri)")
        else:
            info("ORB filter: DISABLED — no opening range capture scheduled")

        scheduler.add_job(
            func               = self._job_update_vix_history,
            trigger            = "cron",
            day_of_week        = "mon-fri",
            hour               = vix_upd_h,
            minute             = vix_upd_m,
            id                 = "vix_history_job",
            name               = f"VIX History Update {cfg.VIX_UPDATE_TIME}",
            misfire_grace_time = 300,
        )
        info(f"VIX history update scheduled at {cfg.VIX_UPDATE_TIME} IST (mon-fri)")

        if cfg.USE_DTE_ENTRY_MAP:
            entry_display = "DTE-map " + ", ".join(
                f"DTE{d}={cfg.DTE_ENTRY_TIME_MAP[d]}" for d in sorted(cfg.DTE_ENTRY_TIME_MAP)
            )
        else:
            entry_display = cfg.ENTRY_TIME
        info(
            f"Scheduler running | Entry: {entry_display} | "
            f"Exit: {cfg.EXIT_TIME} | Monitor: every {cfg.MONITOR_INTERVAL_S}s"
        )
        info("Press Ctrl+C to stop gracefully  |  systemd sends SIGTERM — both handled")
        print("", flush=True)

        # ── SIGTERM handler ────────────────────────────────────────────────────
        def _sigterm_handler(signum, frame):   # noqa: ANN001
            info("SIGTERM received — initiating graceful shutdown")
            raise SystemExit(0)

        try:
            signal.signal(signal.SIGTERM, _sigterm_handler)
        except (OSError, ValueError):
            pass  # Not supported on this platform

        # ── Telegram startup message ───────────────────────────────────────────
        guard_status = (
            f"Margin guard: ENABLED ({int((cfg.MARGIN_BUFFER - 1) * 100)}% buffer, "
            f"fail_open={cfg.MARGIN_GUARD_FAIL_OPEN})"
            if cfg.MARGIN_GUARD_ENABLED else "Margin guard: DISABLED"
        )
        dte_str  = ", ".join(f"DTE{d}" for d in sorted(cfg.TRADE_DTE))
        ivr_tg   = f"IVR>={cfg.IVR_MIN}" if cfg.IVR_FILTER_ENABLED else "off"
        ivp_tg   = f"IVP>={cfg.IVP_MIN}%" if cfg.IVP_FILTER_ENABLED else "off"
        spike_tg = (
            f"VIX spike: +{cfg.VIX_SPIKE_THRESHOLD_PCT}% & "
            f"VIX≥{cfg.VIX_SPIKE_ABS_FLOOR} → exit "
            f"(check/{cfg.VIX_SPIKE_CHECK_INTERVAL_S}s)"
            if cfg.VIX_SPIKE_MONITOR_ENABLED else "VIX spike monitor: off"
        )
        trail_tg = (
            f"Trailing SL: trigger@{cfg.TRAIL_TRIGGER_PCT}% decay  "
            f"lock={cfg.TRAIL_LOCK_PCT}% above LTP"
            if cfg.TRAILING_SL_ENABLED else "Trailing SL: off"
        )
        trade_log_tg = cfg.TRADE_LOG_FILE if cfg.TRADE_LOG_FILE else "off"
        telegram(
            f"🚀 Strategy STARTED v{VERSION} [PARTIAL]\n"
            f"Entry: {entry_display}  Hard Exit: {cfg.EXIT_TIME}\n"
            f"Qty/leg: {cfg.NUMBER_OF_LOTS}×{cfg.LOT_SIZE} = {qty()}\n"
            f"Fixed SL: {cfg.LEG_SL_PERCENT}% per leg  |  {trail_tg}\n"
            f"VIX: {cfg.VIX_MIN}–{cfg.VIX_MAX}  |  {ivr_tg}  |  {ivp_tg}\n"
            f"{spike_tg}\n"
            f"DTE filter: {dte_str}  (trading days, AlgoTest-compatible)\n"
            f"Skip months: "
            f"{', '.join(MONTH_NAMES[m] for m in sorted(cfg.SKIP_MONTHS)) if cfg.SKIP_MONTHS else 'None'}\n"
            f"Target: Rs.{cfg.DAILY_PROFIT_TARGET_PER_LOT}/lot = Rs.{cfg.DAILY_PROFIT_TARGET}  "
            f"Limit: Rs.{cfg.DAILY_LOSS_LIMIT_PER_LOT}/lot = Rs.{cfg.DAILY_LOSS_LIMIT}\n"
            f"Trade log: {trade_log_tg}  |  "
            f"Quote fail alert: {cfg.QUOTE_FAIL_ALERT_THRESHOLD} ticks\n"
            f"{guard_status}"
        )

        try:
            scheduler.start()

        except (KeyboardInterrupt, SystemExit):
            info("Strategy stopped by operator (Ctrl+C / SIGTERM)")
            if state["in_position"]:
                warn(f"Open legs on shutdown: {active_legs()} — closing for safety")
                self._order_engine.close_all(reason="Emergency: Script Stopped by Operator")
            telegram("Strategy STOPPED by operator")
            _flush_telegram(timeout=10)

        except Exception as exc:
            error(f"Scheduler crashed: {exc}")
            if state["in_position"]:
                error(f"Open legs: {active_legs()} — attempting emergency close")
                self._order_engine.close_all(reason="Emergency: Scheduler Crash")
            telegram(f"🚨 Strategy CRASHED\n{exc}\nCheck logs immediately.")
            _flush_telegram(timeout=10)
            raise
