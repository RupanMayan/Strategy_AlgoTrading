"""
Nifty Short Straddle Backtest Engine (Iteration 3)

Runs a day-by-day simulation matching FULL production exit hierarchy:
- Per-leg fixed SL with DTE overrides + dynamic time-of-day tightening
- Trailing SL (activation at decay trigger, ratchet-only tightening)
- Partial square-off (independent per-leg SL)
- Net P&L guard: defer SL when combined position is net profitable (FIX-XX)
- Breakeven SL after partial exit with context awareness (FIX-XXIV)
- Combined premium decay exit (DTE-aware thresholds)
- Asymmetric leg booking (FIX-XXVII)
- Combined profit trailing (FIX-XXVIII)
- Winner-leg early booking (book survivor at deep decay)
- Post-partial recovery lock (FIX-XXV)
- Spot-move / breakeven breach exit
- Daily profit target / loss limit
- Hard exit at 15:15 IST

Usage:
    python nifty_straddle_bt.py
"""

import sys
import json
import logging
from datetime import datetime, time, date, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
from tqdm import tqdm

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import tomli
except ImportError:
    import tomllib as tomli

from nse_holidays import compute_dte, is_trading_day, get_weekly_expiry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Config Loading ─────────────────────────────────────────────────────
def load_config() -> dict:
    config_path = SCRIPT_DIR / "config_backtest.toml"
    with open(config_path, "rb") as f:
        return tomli.load(f)


# ── Dynamic SL Tightening ─────────────────────────────────────────────
def get_dynamic_sl_pct(
    current_time: time,
    base_sl_pct: float,
    schedule: list[dict],
) -> float:
    sorted_sched = sorted(
        schedule,
        key=lambda s: datetime.strptime(s["time"], "%H:%M").time(),
        reverse=True,
    )
    for entry in sorted_sched:
        sched_time = datetime.strptime(entry["time"], "%H:%M").time()
        if current_time >= sched_time:
            return min(entry["sl_pct"], base_sl_pct)
    return base_sl_pct


# ── Effective SL Level (Priority Chain) ────────────────────────────────
def get_effective_sl(
    entry_px: float,
    ltp: float,
    fixed_sl_pct: float,
    trailing_active: bool,
    trailing_sl: float,
    breakeven_active: bool,
    breakeven_sl: float,
    breakeven_grace_elapsed: bool,
) -> tuple[float, str]:
    """
    Return (sl_level, sl_type) using the priority chain:
    1. Trailing SL (if active) — highest priority
    2. Breakeven SL (if active AND grace elapsed AND tighter than fixed)
    3. Fixed/Dynamic SL — default
    """
    fixed_sl = entry_px * (1 + fixed_sl_pct / 100)

    # Priority 1: Trailing SL
    if trailing_active and trailing_sl > 0:
        return trailing_sl, "trailing"

    # Priority 2: Breakeven SL (only if tighter than fixed)
    if breakeven_active and breakeven_sl > 0 and breakeven_grace_elapsed:
        if breakeven_sl < fixed_sl:
            return breakeven_sl, "breakeven"

    # Priority 3: Fixed/Dynamic SL
    return fixed_sl, "fixed"


# ── Single Day Simulation ─────────────────────────────────────────────
def simulate_day(
    day_df: pd.DataFrame,
    trading_date: date,
    config: dict,
) -> dict | None:
    dte = compute_dte(trading_date)
    expiry = get_weekly_expiry(trading_date)
    bt_cfg = config["backtest"]
    risk_cfg = config["risk"]
    timing_cfg = config["timing"]
    instrument_cfg = config["instrument"]

    # ── Filters ──
    if dte not in bt_cfg["trade_dte"]:
        return None
    if trading_date.month in bt_cfg["skip_months"]:
        return None

    # ── Entry time ──
    if timing_cfg.get("use_dte_entry_map", False):
        dte_map = timing_cfg.get("dte_entry_time_map", {})
        entry_str = dte_map.get(str(dte), timing_cfg["entry_time"])
    else:
        entry_str = timing_cfg["entry_time"]

    entry_time = datetime.strptime(entry_str, "%H:%M").time()
    exit_time = datetime.strptime(timing_cfg["exit_time"], "%H:%M").time()

    # ── Find entry candle ──
    day_df = day_df.copy()
    day_df["time"] = day_df["timestamp"].dt.time

    entry_candles = day_df[day_df["time"] >= entry_time]
    if entry_candles.empty:
        return None

    entry_row = entry_candles.iloc[0]
    ce_entry = entry_row.get("ce_close")
    pe_entry = entry_row.get("pe_close")
    spot_at_entry = entry_row.get("spot")

    if pd.isna(ce_entry) or pd.isna(pe_entry) or ce_entry <= 0 or pe_entry <= 0:
        return None

    combined_premium = ce_entry + pe_entry

    # ── Config params ──
    base_sl_pct = risk_cfg.get("dte_sl_override", {}).get(
        str(dte), risk_cfg["leg_sl_percent"]
    )
    dynamic_sl_cfg = risk_cfg.get("dynamic_sl", {})
    dynamic_enabled = dynamic_sl_cfg.get("enabled", False)
    schedule = dynamic_sl_cfg.get("schedule", [])
    slippage_pct = bt_cfg.get("slippage_pct", 0.0)

    lot_size = instrument_cfg["lot_size"]
    num_lots = instrument_cfg["number_of_lots"]
    qty = lot_size * num_lots

    profit_target = risk_cfg["daily_profit_target_per_lot"] * num_lots
    loss_limit = risk_cfg["daily_loss_limit_per_lot"] * num_lots

    # ── Trailing SL config ──
    trail_cfg = risk_cfg.get("trailing_sl", {})
    trail_enabled = trail_cfg.get("enabled", False)
    trail_trigger_pct = trail_cfg.get("trigger_pct", 50.0)
    trail_lock_pct = trail_cfg.get("lock_pct", 15.0)

    # ── Breakeven SL config ──
    be_cfg = risk_cfg.get("breakeven_sl", {})
    be_enabled = be_cfg.get("enabled", False)
    be_grace_min = be_cfg.get("grace_period_min", 5)
    be_buffer_pct = be_cfg.get("buffer_pct", 10.0)

    # ── Combined decay exit config ──
    decay_cfg = risk_cfg.get("combined_decay_exit", {})
    decay_enabled = decay_cfg.get("enabled", False)
    decay_default = decay_cfg.get("decay_target_pct", 60.0)
    decay_dte_override = decay_cfg.get("dte_override", {})
    decay_target = decay_dte_override.get(str(dte), decay_default)

    # ── Winner-leg booking config ──
    winner_cfg = risk_cfg.get("winner_leg_booking", {})
    winner_enabled = winner_cfg.get("enabled", False)
    winner_decay_threshold = winner_cfg.get("decay_threshold_pct", 30.0)

    # ── Asymmetric leg booking config (FIX-XXVII) ──
    asym_cfg = risk_cfg.get("asymmetric_booking", {})
    asym_enabled = asym_cfg.get("enabled", False)
    asym_winner_decay = asym_cfg.get("winner_decay_pct", 40.0)
    asym_loser_intact = asym_cfg.get("loser_intact_pct", 80.0)

    # ── Combined profit trailing config (FIX-XXVIII) ──
    cpt_cfg = risk_cfg.get("combined_profit_trail", {})
    cpt_enabled = cpt_cfg.get("enabled", False)
    cpt_activate_pct = cpt_cfg.get("activate_pct", 30.0)
    cpt_trail_pct = cpt_cfg.get("trail_pct", 40.0)

    # ── Post-partial recovery lock config (FIX-XXV) ──
    recovery_cfg = risk_cfg.get("recovery_lock", {})
    recovery_enabled = recovery_cfg.get("enabled", False)
    recovery_min_rs = recovery_cfg.get("min_recovery_rs_per_lot", 500) * num_lots
    recovery_trail_pct = recovery_cfg.get("trail_pct", 50.0)

    # ── Net P&L guard config (FIX-XX) ──
    npg_cfg = risk_cfg.get("net_pnl_guard", {})
    npg_max_defer_min = npg_cfg.get("max_defer_min", 15)

    # ── Spot-move exit config ──
    sme_cfg = risk_cfg.get("spot_move_exit", {})
    sme_enabled = sme_cfg.get("enabled", False)
    sme_multiplier = sme_cfg.get("spot_multiplier", 1.0)

    # ── Monitor candles ──
    monitor_df = day_df[
        (day_df["timestamp"] > entry_row["timestamp"])
        & (day_df["time"] <= exit_time)
    ]

    # ── Leg state ──
    ce_active = True
    pe_active = True
    ce_exit_price = None
    pe_exit_price = None
    ce_exit_time = None
    pe_exit_time = None
    ce_sl_hit = False
    pe_sl_hit = False
    exit_reason = "hard_exit"

    # Trailing SL state per leg
    ce_trailing_active = False
    pe_trailing_active = False
    ce_trailing_sl = 0.0
    pe_trailing_sl = 0.0
    ce_trail_activated_ts = None
    pe_trail_activated_ts = None
    ce_exit_sl_type = "fixed"
    pe_exit_sl_type = "fixed"

    # Breakeven SL state
    ce_breakeven_active = False
    pe_breakeven_active = False
    ce_breakeven_sl = 0.0
    pe_breakeven_sl = 0.0
    breakeven_armed_time = None

    # Closed P&L accumulator
    closed_pnl = 0.0

    # Combined profit trailing state (FIX-XXVIII)
    cpt_active = False
    cpt_decay_peak = 0.0

    # Post-partial recovery lock state (FIX-XXV)
    recovery_lock_active = False
    recovery_peak_pnl = 0.0

    # Net P&L guard state (FIX-XX)
    ce_defer_start = None
    pe_defer_start = None

    entry_timestamp = entry_row["timestamp"]

    for _, row in monitor_df.iterrows():
        current_time = row["time"]
        current_ts = row["timestamp"]

        # Current LTP for each leg
        ce_ltp = row.get("ce_close", ce_entry) if ce_active else 0
        pe_ltp = row.get("pe_close", pe_entry) if pe_active else 0

        # ── Compute effective fixed/dynamic SL% ──
        if dynamic_enabled:
            eff_sl_pct = get_dynamic_sl_pct(current_time, base_sl_pct, schedule)
        else:
            eff_sl_pct = base_sl_pct

        # ══════════════════════════════════════════════════════════
        # 1. TRAILING SL UPDATE (Phase 1: activation, Phase 2: tighten)
        # ══════════════════════════════════════════════════════════
        if trail_enabled:
            for leg, leg_active, leg_entry, ltp, t_active, t_sl in [
                ("ce", ce_active, ce_entry, ce_ltp, ce_trailing_active, ce_trailing_sl),
                ("pe", pe_active, pe_entry, pe_ltp, pe_trailing_active, pe_trailing_sl),
            ]:
                if not leg_active:
                    continue

                trigger_price = leg_entry * (trail_trigger_pct / 100.0)
                fixed_sl = leg_entry * (1 + eff_sl_pct / 100)

                if not t_active:
                    if ltp <= trigger_price:
                        new_trail_sl = round(ltp * (1 + trail_lock_pct / 100), 2)
                        if new_trail_sl >= fixed_sl:
                            new_trail_sl = fixed_sl
                        if leg == "ce":
                            ce_trailing_active = True
                            ce_trailing_sl = new_trail_sl
                            ce_trail_activated_ts = current_ts
                        else:
                            pe_trailing_active = True
                            pe_trailing_sl = new_trail_sl
                            pe_trail_activated_ts = current_ts
                else:
                    new_trail_sl = round(ltp * (1 + trail_lock_pct / 100), 2)
                    if new_trail_sl < t_sl:
                        if leg == "ce":
                            ce_trailing_sl = new_trail_sl
                        else:
                            pe_trailing_sl = new_trail_sl

        # ══════════════════════════════════════════════════════════
        # 2. PER-LEG SL CHECK (with Net P&L Guard — FIX-XX)
        #    Priority: trailing > breakeven > fixed
        # ══════════════════════════════════════════════════════════

        be_grace_elapsed = False
        if breakeven_armed_time is not None:
            elapsed = (current_ts - breakeven_armed_time).total_seconds() / 60.0
            be_grace_elapsed = elapsed >= be_grace_min

        # CE leg SL check
        if ce_active:
            ce_trail_ready = (
                ce_trailing_active
                and ce_trail_activated_ts is not None
                and current_ts > ce_trail_activated_ts
            )
            sl_level, sl_type = get_effective_sl(
                ce_entry, ce_ltp, eff_sl_pct,
                ce_trail_ready, ce_trailing_sl,
                ce_breakeven_active, ce_breakeven_sl,
                be_grace_elapsed,
            )
            ce_high = row.get("ce_high", 0)
            if not pd.isna(ce_high) and ce_high >= sl_level:
                # Net P&L Guard (FIX-XX): defer if combined position is net positive
                should_defer = False
                if sl_type == "fixed" and closed_pnl != 0:
                    ce_mtm = (ce_entry - ce_ltp) * qty
                    net_pnl = closed_pnl + ce_mtm
                    if net_pnl > 0:
                        if ce_defer_start is None:
                            ce_defer_start = current_ts
                        defer_min = (current_ts - ce_defer_start).total_seconds() / 60.0
                        if defer_min < npg_max_defer_min:
                            should_defer = True

                if not should_defer:
                    ce_exit_price = sl_level * (1 + slippage_pct / 100)
                    ce_exit_time = current_ts
                    ce_active = False
                    ce_sl_hit = True
                    ce_exit_sl_type = sl_type
                    closed_pnl += (ce_entry - ce_exit_price) * qty

                    # Arm breakeven SL on survivor (FIX-XXIV: skip if survivor winning)
                    if be_enabled and pe_active and closed_pnl < 0:
                        survivor_is_winner = (pe_ltp > 0 and pe_ltp < pe_entry)
                        if not survivor_is_winner:
                            raw_be = pe_entry + closed_pnl / qty
                            # Production direction: buffer UP
                            be_price = round(raw_be * (1 + be_buffer_pct / 100), 2)
                            if 0 < be_price < pe_entry * (1 + eff_sl_pct / 100):
                                pe_breakeven_active = True
                                pe_breakeven_sl = be_price
                                breakeven_armed_time = current_ts

        # PE leg SL check
        if pe_active:
            pe_trail_ready = (
                pe_trailing_active
                and pe_trail_activated_ts is not None
                and current_ts > pe_trail_activated_ts
            )
            sl_level, sl_type = get_effective_sl(
                pe_entry, pe_ltp, eff_sl_pct,
                pe_trail_ready, pe_trailing_sl,
                pe_breakeven_active, pe_breakeven_sl,
                be_grace_elapsed,
            )
            pe_high = row.get("pe_high", 0)
            if not pd.isna(pe_high) and pe_high >= sl_level:
                # Net P&L Guard (FIX-XX): defer if combined position is net positive
                should_defer = False
                if sl_type == "fixed" and closed_pnl != 0:
                    pe_mtm = (pe_entry - pe_ltp) * qty
                    net_pnl = closed_pnl + pe_mtm
                    if net_pnl > 0:
                        if pe_defer_start is None:
                            pe_defer_start = current_ts
                        defer_min = (current_ts - pe_defer_start).total_seconds() / 60.0
                        if defer_min < npg_max_defer_min:
                            should_defer = True

                if not should_defer:
                    pe_exit_price = sl_level * (1 + slippage_pct / 100)
                    pe_exit_time = current_ts
                    pe_active = False
                    pe_sl_hit = True
                    pe_exit_sl_type = sl_type
                    closed_pnl += (pe_entry - pe_exit_price) * qty

                    # Arm breakeven SL on survivor (FIX-XXIV: skip if survivor winning)
                    if be_enabled and ce_active and closed_pnl < 0:
                        survivor_is_winner = (ce_ltp > 0 and ce_ltp < ce_entry)
                        if not survivor_is_winner:
                            raw_be = ce_entry + closed_pnl / qty
                            # Production direction: buffer UP
                            be_price = round(raw_be * (1 + be_buffer_pct / 100), 2)
                            if 0 < be_price < ce_entry * (1 + eff_sl_pct / 100):
                                ce_breakeven_active = True
                                ce_breakeven_sl = be_price
                                breakeven_armed_time = current_ts

        # ══════════════════════════════════════════════════════════
        # 3. COMBINED DECAY EXIT (both legs active)
        # ══════════════════════════════════════════════════════════
        if decay_enabled and ce_active and pe_active:
            combined_current = ce_ltp + pe_ltp
            if combined_premium > 0:
                decay_pct = (1 - combined_current / combined_premium) * 100
                if decay_pct >= decay_target:
                    ce_exit_price = ce_ltp
                    pe_exit_price = pe_ltp
                    ce_exit_time = current_ts
                    pe_exit_time = current_ts
                    ce_active = False
                    pe_active = False
                    exit_reason = f"combined_decay_{decay_pct:.0f}pct"
                    break

        # ══════════════════════════════════════════════════════════
        # 4. ASYMMETRIC LEG BOOKING (FIX-XXVII, both legs active)
        # ══════════════════════════════════════════════════════════
        if asym_enabled and ce_active and pe_active:
            ce_pct = (ce_ltp / ce_entry) * 100 if ce_entry > 0 else 100
            pe_pct = (pe_ltp / pe_entry) * 100 if pe_entry > 0 else 100

            if ce_pct <= asym_winner_decay and pe_pct >= asym_loser_intact:
                ce_exit_price = ce_ltp
                ce_exit_time = current_ts
                ce_active = False
                closed_pnl += (ce_entry - ce_exit_price) * qty
                exit_reason = f"asymmetric_book_ce_{ce_pct:.0f}pct"
            elif pe_pct <= asym_winner_decay and ce_pct >= asym_loser_intact:
                pe_exit_price = pe_ltp
                pe_exit_time = current_ts
                pe_active = False
                closed_pnl += (pe_entry - pe_exit_price) * qty
                exit_reason = f"asymmetric_book_pe_{pe_pct:.0f}pct"

        # ══════════════════════════════════════════════════════════
        # 5. COMBINED PROFIT TRAILING (FIX-XXVIII, both legs active)
        # ══════════════════════════════════════════════════════════
        if cpt_enabled and ce_active and pe_active:
            combined_current = ce_ltp + pe_ltp
            if combined_premium > 0:
                decay_pct = round((1 - combined_current / combined_premium) * 100, 2)
                if not cpt_active:
                    if decay_pct >= cpt_activate_pct:
                        cpt_active = True
                        cpt_decay_peak = decay_pct
                else:
                    if decay_pct > cpt_decay_peak:
                        cpt_decay_peak = decay_pct
                    retracement = cpt_decay_peak - decay_pct
                    if retracement >= cpt_trail_pct:
                        ce_exit_price = ce_ltp
                        pe_exit_price = pe_ltp
                        ce_exit_time = current_ts
                        pe_exit_time = current_ts
                        ce_active = False
                        pe_active = False
                        exit_reason = f"combined_trail_retrace_{retracement:.0f}pts"
                        break

        # ══════════════════════════════════════════════════════════
        # 6. WINNER-LEG EARLY BOOKING (single survivor, deep decay)
        # ══════════════════════════════════════════════════════════
        if winner_enabled:
            active_count = int(ce_active) + int(pe_active)
            if active_count == 1:
                if ce_active:
                    winner_decay = (ce_ltp / ce_entry) * 100
                    if winner_decay <= winner_decay_threshold:
                        ce_exit_price = ce_ltp
                        ce_exit_time = current_ts
                        ce_active = False
                        exit_reason = f"winner_booking_ce_{winner_decay:.0f}pct"
                        break
                elif pe_active:
                    winner_decay = (pe_ltp / pe_entry) * 100
                    if winner_decay <= winner_decay_threshold:
                        pe_exit_price = pe_ltp
                        pe_exit_time = current_ts
                        pe_active = False
                        exit_reason = f"winner_booking_pe_{winner_decay:.0f}pct"
                        break

        # ══════════════════════════════════════════════════════════
        # 7. COMBINED P&L + POST-PARTIAL RECOVERY LOCK (FIX-XXV)
        # ══════════════════════════════════════════════════════════
        ce_current = ce_exit_price if not ce_active else ce_ltp
        pe_current = pe_exit_price if not pe_active else pe_ltp
        running_pnl = (ce_entry - ce_current) * qty + (pe_entry - pe_current) * qty

        # Recovery lock: trail recovery peak after partial exit at loss
        if recovery_enabled and closed_pnl < 0:
            active_count = int(ce_active) + int(pe_active)
            if active_count == 1:
                if running_pnl > 0:
                    if not recovery_lock_active:
                        if running_pnl >= recovery_min_rs:
                            recovery_lock_active = True
                            recovery_peak_pnl = running_pnl
                    else:
                        if running_pnl > recovery_peak_pnl:
                            recovery_peak_pnl = running_pnl
                        if recovery_peak_pnl > 0:
                            retrace_pct = (recovery_peak_pnl - running_pnl) / recovery_peak_pnl * 100
                            if retrace_pct >= recovery_trail_pct:
                                if ce_active:
                                    ce_exit_price = ce_ltp
                                    ce_exit_time = current_ts
                                    ce_active = False
                                if pe_active:
                                    pe_exit_price = pe_ltp
                                    pe_exit_time = current_ts
                                    pe_active = False
                                exit_reason = f"recovery_lock_{retrace_pct:.0f}pct"
                                break

        # ══════════════════════════════════════════════════════════
        # 8. SPOT-MOVE / BREAKEVEN BREACH EXIT
        # ══════════════════════════════════════════════════════════
        if sme_enabled and (ce_active or pe_active):
            current_spot = row.get("spot", 0)
            if current_spot > 0 and spot_at_entry > 0:
                # Use active-leg premium only (production FIX-XVI)
                active_premium = 0
                if ce_active:
                    active_premium += ce_entry
                if pe_active:
                    active_premium += pe_entry
                if active_premium == 0:
                    active_premium = combined_premium
                move_threshold = active_premium * sme_multiplier
                move_abs = abs(current_spot - spot_at_entry)
                if move_abs >= move_threshold:
                    if ce_active:
                        ce_exit_price = ce_ltp
                        ce_exit_time = current_ts
                        ce_active = False
                    if pe_active:
                        pe_exit_price = pe_ltp
                        pe_exit_time = current_ts
                        pe_active = False
                    exit_reason = f"spot_move_{move_abs:.0f}pts"
                    break

        # ══════════════════════════════════════════════════════════
        # 9. DAILY LIMITS
        # ══════════════════════════════════════════════════════════
        if running_pnl >= profit_target:
            if ce_active:
                ce_exit_price = ce_ltp
                ce_exit_time = current_ts
                ce_active = False
            if pe_active:
                pe_exit_price = pe_ltp
                pe_exit_time = current_ts
                pe_active = False
            exit_reason = "profit_target"
            break

        if running_pnl <= loss_limit:
            if ce_active:
                ce_exit_price = ce_ltp
                ce_exit_time = current_ts
                ce_active = False
            if pe_active:
                pe_exit_price = pe_ltp
                pe_exit_time = current_ts
                pe_active = False
            exit_reason = "loss_limit"
            break

        # Both legs closed — stop monitoring
        if not ce_active and not pe_active:
            if not exit_reason or exit_reason == "hard_exit":
                exit_reason = "both_sl_hit"
            break

    # ── Hard exit at 15:15 ──
    if ce_active or pe_active:
        exit_candles = day_df[day_df["time"] >= exit_time]
        if not exit_candles.empty:
            exit_row = exit_candles.iloc[0]
            if ce_active:
                ce_exit_price = exit_row.get("ce_close", ce_entry)
                ce_exit_time = exit_row["timestamp"]
                ce_active = False
            if pe_active:
                pe_exit_price = exit_row.get("pe_close", pe_entry)
                pe_exit_time = exit_row["timestamp"]
                pe_active = False
        else:
            last_row = day_df.iloc[-1]
            if ce_active:
                ce_exit_price = last_row.get("ce_close", ce_entry)
                ce_exit_time = last_row["timestamp"]
            if pe_active:
                pe_exit_price = last_row.get("pe_close", pe_entry)
                pe_exit_time = last_row["timestamp"]

    # ── Calculate P&L ──
    ce_pnl = (ce_entry - ce_exit_price) * qty
    pe_pnl = (pe_entry - pe_exit_price) * qty
    total_pnl = ce_pnl + pe_pnl

    return {
        "date": trading_date.isoformat(),
        "expiry": expiry.isoformat(),
        "dte": dte,
        "spot_at_entry": round(spot_at_entry, 2),
        "ce_entry": round(ce_entry, 2),
        "pe_entry": round(pe_entry, 2),
        "combined_premium": round(combined_premium, 2),
        "ce_exit": round(ce_exit_price, 2),
        "pe_exit": round(pe_exit_price, 2),
        "ce_sl_hit": ce_sl_hit,
        "pe_sl_hit": pe_sl_hit,
        "ce_pnl": round(ce_pnl, 2),
        "pe_pnl": round(pe_pnl, 2),
        "total_pnl": round(total_pnl, 2),
        "exit_reason": exit_reason,
        "ce_trailing_activated": ce_trailing_active,
        "pe_trailing_activated": pe_trailing_active,
        "ce_exit_sl_type": ce_exit_sl_type,
        "pe_exit_sl_type": pe_exit_sl_type,
        "entry_time": entry_str,
        "ce_exit_time": str(ce_exit_time) if ce_exit_time else None,
        "pe_exit_time": str(pe_exit_time) if pe_exit_time else None,
    }


# ── Run Full Backtest ──────────────────────────────────────────────────
def run_backtest(config: dict) -> pd.DataFrame:
    data_path = SCRIPT_DIR / "data" / "nifty_options_2025" / "nifty_atm_weekly_1min.parquet"
    if not data_path.exists():
        log.error(f"Data file not found: {data_path}")
        log.error("Run dhan_data_fetcher.py first to download data.")
        sys.exit(1)

    df = pd.read_parquet(data_path)
    log.info(f"Loaded {len(df):,} rows from {data_path}")

    if df["timestamp"].dt.tz is None:
        df["timestamp"] = df["timestamp"].dt.tz_localize("Asia/Kolkata")

    df["trading_date"] = df["timestamp"].dt.date
    grouped = df.groupby("trading_date")

    trades = []
    for trading_date, day_df in tqdm(grouped, desc="Backtesting"):
        if not is_trading_day(trading_date):
            continue
        result = simulate_day(day_df, trading_date, config)
        if result:
            trades.append(result)

    if not trades:
        log.error("No trades generated!")
        sys.exit(1)

    trades_df = pd.DataFrame(trades)
    log.info(f"Generated {len(trades_df)} trades")
    return trades_df


# ── Analytics ──────────────────────────────────────────────────────────
def generate_analytics(trades_df: pd.DataFrame, config: dict):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_dir = SCRIPT_DIR / "output"
    charts_dir = output_dir / "charts"
    output_dir.mkdir(parents=True, exist_ok=True)
    charts_dir.mkdir(parents=True, exist_ok=True)

    lot_size = config["instrument"]["lot_size"]
    num_lots = config["instrument"]["number_of_lots"]

    trades_df.to_csv(output_dir / "bt_trades.csv", index=False)
    log.info(f"Trade log saved to {output_dir / 'bt_trades.csv'}")

    trades_df["date"] = pd.to_datetime(trades_df["date"])
    trades_df = trades_df.sort_values("date").reset_index(drop=True)
    trades_df["cumulative_pnl"] = trades_df["total_pnl"].cumsum()

    # ── Summary Statistics ──
    total_pnl = trades_df["total_pnl"].sum()
    num_trades = len(trades_df)
    winners = trades_df[trades_df["total_pnl"] > 0]
    losers = trades_df[trades_df["total_pnl"] < 0]
    win_rate = len(winners) / num_trades * 100 if num_trades > 0 else 0
    avg_win = winners["total_pnl"].mean() if len(winners) > 0 else 0
    avg_loss = losers["total_pnl"].mean() if len(losers) > 0 else 0
    profit_factor = (
        abs(winners["total_pnl"].sum() / losers["total_pnl"].sum())
        if len(losers) > 0 and losers["total_pnl"].sum() != 0
        else float("inf")
    )

    cumulative = trades_df["cumulative_pnl"]
    running_max = cumulative.cummax()
    drawdown = cumulative - running_max
    max_drawdown = drawdown.min()
    max_drawdown_pct = (max_drawdown / running_max[drawdown.idxmin()] * 100
                        if running_max[drawdown.idxmin()] != 0 else 0)

    daily_returns = trades_df["total_pnl"]
    trades_per_year = len(daily_returns)
    sharpe = (
        (daily_returns.mean() / daily_returns.std()) * np.sqrt(trades_per_year)
        if daily_returns.std() > 0 else 0
    )
    downside = daily_returns[daily_returns < 0]
    sortino = (
        (daily_returns.mean() / downside.std()) * np.sqrt(trades_per_year)
        if len(downside) > 0 and downside.std() > 0 else 0
    )
    calmar = abs(total_pnl / max_drawdown) if max_drawdown != 0 else 0

    # SL hit analysis
    ce_sl_hits = trades_df["ce_sl_hit"].sum()
    pe_sl_hits = trades_df["pe_sl_hit"].sum()
    both_sl_hits = ((trades_df["ce_sl_hit"]) & (trades_df["pe_sl_hit"])).sum()
    no_sl_hits = ((~trades_df["ce_sl_hit"]) & (~trades_df["pe_sl_hit"])).sum()

    # Exit reason breakdown (group by category)
    def categorize_exit(reason):
        if reason.startswith("recovery_lock"):
            return "recovery_lock"
        if reason.startswith("spot_move"):
            return "spot_move"
        if reason.startswith("combined_decay"):
            return "combined_decay"
        if reason.startswith("winner_booking"):
            return "winner_booking"
        if reason.startswith("asymmetric_book"):
            return "asymmetric_booking"
        if reason.startswith("combined_trail"):
            return "combined_trail"
        return reason

    trades_df["exit_category"] = trades_df["exit_reason"].apply(categorize_exit)
    exit_reasons = trades_df["exit_category"].value_counts().to_dict()

    # Per-DTE breakdown
    dte_stats = trades_df.groupby("dte").agg(
        count=("total_pnl", "count"),
        total_pnl=("total_pnl", "sum"),
        avg_pnl=("total_pnl", "mean"),
        win_rate=("total_pnl", lambda x: (x > 0).sum() / len(x) * 100),
    ).to_dict(orient="index")

    summary = {
        "iteration": 3,
        "features": "full_production_parity",
        "total_pnl": round(total_pnl, 2),
        "num_trades": num_trades,
        "win_rate_pct": round(win_rate, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 4),
        "max_drawdown": round(max_drawdown, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "sharpe_ratio": round(sharpe, 4),
        "sortino_ratio": round(sortino, 4),
        "calmar_ratio": round(calmar, 4),
        "ce_sl_hits": int(ce_sl_hits),
        "pe_sl_hits": int(pe_sl_hits),
        "both_sl_hits": int(both_sl_hits),
        "no_sl_days": int(no_sl_hits),
        "exit_reasons": exit_reasons,
        "per_dte": {str(k): {kk: round(vv, 2) for kk, vv in v.items()} for k, v in dte_stats.items()},
        "lot_size": lot_size,
        "num_lots": num_lots,
        "backtest_period": f"{config['backtest']['from_date']} to {config['backtest']['to_date']}",
    }

    with open(output_dir / "bt_summary.json", "w") as f:
        json.dump(summary, f, indent=2)
    log.info(f"Summary saved to {output_dir / 'bt_summary.json'}")

    # ── Charts ──

    # 1. Equity Curve
    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(trades_df["date"], trades_df["cumulative_pnl"], linewidth=1.5, color="blue")
    ax.fill_between(trades_df["date"], trades_df["cumulative_pnl"], alpha=0.15, color="blue")
    ax.axhline(y=0, color="gray", linestyle="--", alpha=0.5)
    ax.set_title(f"Equity Curve — Nifty Short Straddle v3 (Total P&L: ₹{total_pnl:,.0f})")
    ax.set_xlabel("Date")
    ax.set_ylabel("Cumulative P&L (₹)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(charts_dir / "equity_curve.png", dpi=150)
    plt.close(fig)

    # 2. Drawdown Chart
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.fill_between(trades_df["date"], drawdown, color="red", alpha=0.4)
    ax.set_title(f"Drawdown (Max: ₹{max_drawdown:,.0f})")
    ax.set_xlabel("Date")
    ax.set_ylabel("Drawdown (₹)")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(charts_dir / "drawdown.png", dpi=150)
    plt.close(fig)

    # 3. Monthly Returns Heatmap
    trades_df["year"] = trades_df["date"].dt.year
    trades_df["month"] = trades_df["date"].dt.month
    monthly = trades_df.groupby(["year", "month"])["total_pnl"].sum().unstack(fill_value=0)

    fig, ax = plt.subplots(figsize=(14, 4))
    im = ax.imshow(monthly.values, cmap="RdYlGn", aspect="auto")
    ax.set_xticks(range(len(monthly.columns)))
    ax.set_xticklabels([f"M{m}" for m in monthly.columns])
    ax.set_yticks(range(len(monthly.index)))
    ax.set_yticklabels(monthly.index)
    for i in range(len(monthly.index)):
        for j in range(len(monthly.columns)):
            val = monthly.values[i, j]
            ax.text(j, i, f"₹{val:,.0f}", ha="center", va="center", fontsize=8,
                    color="black" if abs(val) < monthly.values.max() * 0.5 else "white")
    ax.set_title("Monthly P&L Heatmap (Iteration 3)")
    fig.colorbar(im, ax=ax)
    fig.tight_layout()
    fig.savefig(charts_dir / "monthly_heatmap.png", dpi=150)
    plt.close(fig)

    # 4. Per-DTE Performance
    dte_df = trades_df.groupby("dte").agg(
        total=("total_pnl", "sum"),
        avg=("total_pnl", "mean"),
        count=("total_pnl", "count"),
    ).reset_index()

    fig, axes = plt.subplots(1, 2, figsize=(14, 5))
    axes[0].bar(dte_df["dte"], dte_df["total"], color=["green" if x > 0 else "red" for x in dte_df["total"]])
    axes[0].set_title("Total P&L by DTE")
    axes[0].set_xlabel("DTE")
    axes[0].set_ylabel("Total P&L (₹)")
    axes[0].grid(True, alpha=0.3)

    axes[1].bar(dte_df["dte"], dte_df["avg"], color=["green" if x > 0 else "red" for x in dte_df["avg"]])
    axes[1].set_title("Avg P&L per Trade by DTE")
    axes[1].set_xlabel("DTE")
    axes[1].set_ylabel("Avg P&L (₹)")
    axes[1].grid(True, alpha=0.3)

    fig.suptitle("Performance by Days-to-Expiry (Iteration 3)")
    fig.tight_layout()
    fig.savefig(charts_dir / "dte_breakdown.png", dpi=150)
    plt.close(fig)

    # 5. Exit Reason Pie Chart (new in Iteration 2)
    fig, ax = plt.subplots(figsize=(8, 8))
    reasons = trades_df["exit_reason"].value_counts()
    ax.pie(reasons.values, labels=reasons.index, autopct="%1.1f%%", startangle=90)
    ax.set_title("Exit Reason Distribution")
    fig.tight_layout()
    fig.savefig(charts_dir / "exit_reasons.png", dpi=150)
    plt.close(fig)

    # ── Console Summary ──
    print(f"\n{'='*60}")
    print(f"  BACKTEST RESULTS — Nifty Short Straddle (Iteration 3)")
    print(f"  Full Production Parity: All FIX-XX to FIX-XXVIII")
    print(f"{'='*60}")
    print(f"  Period:         {config['backtest']['from_date']} → {config['backtest']['to_date']}")
    print(f"  Total Trades:   {num_trades}")
    print(f"  Total P&L:      ₹{total_pnl:,.2f}")
    print(f"  Win Rate:       {win_rate:.1f}%")
    print(f"  Avg Win:        ₹{avg_win:,.2f}")
    print(f"  Avg Loss:       ₹{avg_loss:,.2f}")
    print(f"  Profit Factor:  {profit_factor:.2f}")
    print(f"  Max Drawdown:   ₹{max_drawdown:,.2f}")
    print(f"  Sharpe Ratio:   {sharpe:.2f}")
    print(f"  Sortino Ratio:  {sortino:.2f}")
    print(f"  Calmar Ratio:   {calmar:.2f}")
    print(f"{'─'*60}")
    print(f"  CE SL Hits:     {ce_sl_hits} ({ce_sl_hits/num_trades*100:.1f}%)")
    print(f"  PE SL Hits:     {pe_sl_hits} ({pe_sl_hits/num_trades*100:.1f}%)")
    print(f"  Both SL Hit:    {both_sl_hits} ({both_sl_hits/num_trades*100:.1f}%)")
    print(f"  No SL (decay):  {no_sl_hits} ({no_sl_hits/num_trades*100:.1f}%)")
    print(f"{'─'*60}")
    print(f"  Exit Reasons:")
    for reason, count in sorted(exit_reasons.items(), key=lambda x: -x[1]):
        print(f"    {reason}: {count} ({count/num_trades*100:.1f}%)")
    print(f"{'─'*60}")
    print(f"  Per-DTE:")
    for dte_val, stats in sorted(dte_stats.items()):
        print(f"    DTE {dte_val}: {stats['count']:.0f} trades, ₹{stats['total_pnl']:,.0f} total, "
              f"₹{stats['avg_pnl']:,.0f} avg, {stats['win_rate']:.0f}% win")
    print(f"{'='*60}")
    print(f"\n  Output: {output_dir}/")
    print(f"  Charts: {charts_dir}/")


# ── Main ───────────────────────────────────────────────────────────────
def main():
    config = load_config()
    trades_df = run_backtest(config)
    generate_analytics(trades_df, config)


if __name__ == "__main__":
    main()
