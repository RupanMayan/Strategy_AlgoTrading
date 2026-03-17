"""
╔══════════════════════════════════════════════════════════════════════════════════╗
║   NIFTY TRENDING STRATEGY  —  PARTIAL SQUARE OFF   v5.2.0                     ║
║   Short ATM Straddle  |  Weekly Expiry  |  Intraday MIS                        ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║   Backtest Results  (AlgoTest 2019–2026  |  1746 trades  |  PARTIAL mode)      ║
║   Total P&L  : Rs.5,04,192  (qty 65)  →  scaled ~Rs.5,81,000 at qty 75        ║
║   Win Rate   : 66.71%   |  Avg/trade  : Rs.289                                 ║
║   Max DD     : Rs.34,179 (AlgoTest reported)                                   ║
║   Return/MDD : 1.38     |  Reward:Risk: 1.09                                   ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║   PARTIAL SQUARE OFF — EXACT LOGIC (verified from 1746-trade AlgoTest CSV)     ║
║                                                                                  ║
║   Key facts from CSV analysis:                                                  ║
║   • 61.6% of trades: one leg SL fires, other leg survives to 15:15             ║
║   • 35.4% of trades: both legs SL — at DIFFERENT times (independent)           ║
║   •  3.0% of trades: both legs exit normally at 15:15                          ║
║   • 99.6% of SL exits hit at exactly 20.0% of each leg's entry premium         ║
║   • Median SL hit: 22 min after entry | 40% of SLs hit within 15 min           ║
║                                                                                  ║
║   Implementation:                                                               ║
║   1. Entry  : SELL CE + SELL PE at ATM simultaneously at 09:17                 ║
║   2. Per-leg: each leg monitored with its OWN independent 20% SL level         ║
║              CE_SL = CE_entry_price × 1.20                                     ║
║              PE_SL = PE_entry_price × 1.20                                     ║
║   3. CE hits SL → BUY CE only. PE continues with its own SL                   ║
║   4. PE hits SL → BUY PE only. CE continues with its own SL                   ║
║   5. Surviving leg exits at: its own SL  OR  15:15 hard exit                  ║
║   6. Daily target/limit evaluated on COMBINED P&L:                             ║
║              combined = closed_leg_pnl + open_leg_mtm                          ║
║      If either breaches → close ALL remaining open legs                        ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║   v5.0.0 AUDIT FIXES  (full code review from v4.2.1)                           ║
║                                                                                  ║
║   FIX-1  get_dte() calls get_expiry() which calls nearest_tuesday_expiry()     ║
║          which logs "Auto expiry: …" on EVERY monitor tick (every 15s).        ║
║          Fixed: split _compute_expiry_date() as a silent helper; logging        ║
║          only in get_expiry() for entry-path calls.                             ║
║                                                                                  ║
║   FIX-2  _print_banner() calls get_expiry() (via _dte_to_dayname) once per     ║
║          DTE value in TRADE_DTE — O(n) API+log calls at startup.               ║
║          Fixed: compute expiry once, pass to _dte_to_dayname().                 ║
║                                                                                  ║
║   FIX-3  close_all() — when both legs active, uses closeposition() then        ║
║          calls _mark_fully_flat() which uses state["closed_pnl"] as final      ║
║          P&L. But closed_pnl is only updated by close_one_leg(), not by        ║
║          closeposition(). So final P&L = 0.0 (wrong).                          ║
║          Fixed: add open_mtm snapshot to closed_pnl before calling             ║
║          _mark_fully_flat() when using the atomic closeposition() path.         ║
║                                                                                  ║
║   FIX-4  _capture_fill_prices() sleeps AFTER the last attempt (attempt==3      ║
║          still calls time.sleep()). The guard `if attempt < MAX_ATTEMPTS`       ║
║          was correct in v4.0.0 but a refactor broke it. Verified correct        ║
║          in this version — no change needed.                                    ║
║                                                                                  ║
║   FIX-5  reconcile Case B restores state["trade_count"] from saved JSON.       ║
║          This is correct — trade_count persists across the day.                 ║
║          But if the script runs the session stats across days (no restart),     ║
║          trade_count was ALSO persisted. Verified acceptable — no change.       ║
║                                                                                  ║
║   FIX-6  job_entry() resets state["today_pnl"] and state["closed_pnl"] to 0   ║
║          AFTER dte_filter_ok() returns True, but BEFORE place_entry().         ║
║          If place_entry() fails, counters are reset but no trade happened.      ║
║          This is safe — counters are reset again on next valid entry. OK.       ║
║                                                                                  ║
║   FIX-7  dte_filter_ok() calls get_dte() which calls get_expiry() which        ║
║          calls nearest_tuesday_expiry() which logs. On non-trade days this      ║
║          log is harmless but verbose. Accepted as-is (filter order is cheap).   ║
║                                                                                  ║
║   FIX-8  close_all() with ONE active leg calls close_one_leg() with no         ║
║          current_ltp (defaults to 0.0), so approx_pnl = 0.0 and closed_pnl    ║
║          += 0. This means _mark_fully_flat() uses closed_pnl without the        ║
║          last leg's approx P&L for the final summary.                           ║
║          Fixed: fetch LTP before calling close_one_leg() in single-leg path.   ║
║                                                                                  ║
║   FIX-9  NSE VIX fallback uses requests.Session() without setting cookies or   ║
║          headers that NSE now requires. This path was already fail-safe.        ║
║          Added explicit cookie-grabbing step and improved resilience.           ║
║                                                                                  ║
║   FIX-10 _mark_fully_flat() clears state["today_pnl"] = final_pnl then        ║
║          immediately resets all state including today_pnl = 0.0 WAIT —         ║
║          today_pnl is NOT in the reset block. Confirmed: today_pnl is set to   ║
║          final_pnl but never cleared. After flat, today_pnl shows yesterday's  ║
║          value. Fixed: reset today_pnl = 0.0 in reset block.                   ║
║                                                                                  ║
║   FIX-11 In dte_filter_ok(), telegram() is called only on SKIP_MONTHS.        ║
║          DTE-filtered skip days produce no Telegram alert. This is intentional ║
║          (DTE filter fires every non-trade day). No change.                     ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║   v5.2.0 PRODUCTION-GRADE HARDENING  (third-pass audit)                         ║
║                                                                                  ║
║   FIX-I   CRITICAL — date.today() used in 3 places instead of                  ║
║           now_ist().date(). On UTC servers (VPS, cloud) date.today()            ║
║           returns UTC date, which can be 5h 30m behind IST. This caused        ║
║           wrong expiry dates late in the IST evening, and wrong stale-state     ║
║           detection on midnight boundaries. All 3 locations fixed:              ║
║             _nearest_tuesday_date(), get_dte(), reconcile_on_startup()          ║
║                                                                                  ║
║   FIX-II  Thread safety: _monitor_lock upgraded from threading.Lock() to       ║
║           threading.RLock(). close_all() now acquires _monitor_lock (blocking)  ║
║           before entering _close_all_locked(). This prevents a race where       ║
║           job_exit() fires at 15:15 while a monitor tick is mid-SL-close,      ║
║           which could result in duplicate close orders on a leg.                ║
║           close_all() → _close_all_locked() split for clean re-entrancy.        ║
║                                                                                  ║
║   FIX-III NSE holiday handling delegated to OpenAlgo Python Strategy            ║
║           scheduler. OpenAlgo already skips NSE market holidays, so the         ║
║           internal NSE_HOLIDAYS calendar is unnecessary and has been removed.   ║
║           get_dte() now skips weekends only (Sat/Sun). The script will never    ║
║           start on a market holiday because OpenAlgo won't schedule it.         ║
║                                                                                  ║
║   FIX-IV  _capture_fill_prices() retries increased from 3×1s to 5×(1-4s)      ║
║           exponential back-off. On high-VIX days, broker fill reporting can     ║
║           be delayed beyond 3s. If capture still fails, a Telegram alert is     ║
║           sent immediately so the operator knows SL is disabled.                ║
║                                                                                  ║
║   FIX-V   _validate_config() added — called at startup before any API call.    ║
║           Checks 15+ configuration constraints and raises ValueError with a      ║
║           clear error list if anything is misconfigured. Prevents a live trade  ║
║           being placed with typos (e.g. positive DAILY_LOSS_LIMIT).             ║
║                                                                                  ║
║   FIX-VI  SIGTERM handler added in run(). Raises SystemExit(0) on SIGTERM so   ║
║           the scheduler's except (KeyboardInterrupt, SystemExit) block runs     ║
║           and positions are closed before the process exits. Required for        ║
║           clean shutdown under systemd / docker stop / supervisord.             ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║   v5.1.0 FIXES  (second-pass audit)                                             ║
║                                                                                  ║
║   FIX-A  Double get_expiry() call: job_entry() resolved expiry for margin      ║
║          check, then place_entry() called get_expiry() again independently.     ║
║          Two "Auto expiry" log lines per trade day; tiny race window if the     ║
║          Tuesday 15:30 boundary falls between the two calls (theoretical).      ║
║          Fixed: place_entry(expiry) now accepts expiry as a parameter.          ║
║          job_entry() resolves expiry ONCE, passes it to both                   ║
║          check_margin_sufficient() and place_entry(). Single source of truth.  ║
║                                                                                  ║
║   FIX-B  FIX-3 edge case: if no monitor tick has run yet when close_all()      ║
║          fires (window: 0–15s after entry), today_pnl=0 so open_mtm_snapshot   ║
║          = 0-0 = 0, meaning final P&L shows Rs.0 despite live position value.  ║
║          Fixed: close_all() now detects the no-tick case (today_pnl==0 with    ║
║          both legs active + entry prices captured) and fetches live LTPs        ║
║          directly to compute a real open_mtm. Falls back gracefully to the      ║
║          existing today_pnl snapshot if either LTP fetch fails.                ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║   DTE REFERENCE (NIFTY, Tuesday expiry, trading days per AlgoTest)             ║
║   DTE0 = Tuesday   expiry day          — peak theta                            ║
║   DTE1 = Monday    1 trading day out   — strong theta                          ║
║   DTE2 = Friday    2 trading days out  — moderate theta                        ║
║   DTE3 = Thursday  3 trading days out  — lower premium                         ║
║   DTE4 = Wednesday 4 trading days out  — thin premium                          ║
║   DTE5 = Tuesday   5 trading days out  — previous week expiry day              ║
║   DTE6 = Monday    6 trading days out  — previous week Monday                  ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║   QUICK START                                                                    ║
║   1.  pip install openalgo apscheduler pytz requests                            ║
║   2.  Set environment variables (or fill directly in SECTION 1 and 10):        ║
║       export OPENALGO_APIKEY="your_key"                                         ║
║       export TELEGRAM_BOT_TOKEN="your_token"                                    ║
║       export TELEGRAM_CHAT_ID="your_chat_id"                                    ║
║   3.  Sync Master Contract in OpenAlgo dashboard before 09:00                  ║
║   4.  Enable Analyze Mode in OpenAlgo dashboard (paper trade first)            ║
║   5.  python Nifty_TrendingStrategy_DTE_v5.py                                  ║
║   6.  After satisfied with paper trades → disable Analyze Mode → goes LIVE     ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║   SYMBOL FORMAT  (docs.openalgo.in)                                             ║
║   Order entry   : NIFTY  on  NSE_INDEX  (OpenAlgo resolves ATM strike)         ║
║   Option quotes : NIFTY25MAR2623000CE  on  NFO  (Tuesday expiry)               ║
║   VIX           : INDIAVIX  on  NSE_INDEX                                      ║
╚══════════════════════════════════════════════════════════════════════════════════╝
"""

import os
import json
import time
import signal
import tempfile
import threading
import requests
import pytz
from datetime import datetime, date, timedelta

# Third-party  (pip install openalgo apscheduler pytz)
from openalgo import api as OpenAlgoClient
from apscheduler.schedulers.blocking import BlockingScheduler


# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — OPENALGO CONNECTION
# ═══════════════════════════════════════════════════════════════════════════════

OPENALGO_HOST    = "http://127.0.0.1:5000"
OPENALGO_API_KEY = os.getenv("OPENALGO_APIKEY", "your_openalgo_api_key_here")

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — INSTRUMENT
# ═══════════════════════════════════════════════════════════════════════════════

UNDERLYING     = "NIFTY"       # NIFTY | BANKNIFTY | FINNIFTY
EXCHANGE       = "NSE_INDEX"   # Always NSE_INDEX for index-based option orders
LOT_SIZE       = 65            # NIFTY=65  BANKNIFTY=35  FINNIFTY=40
NUMBER_OF_LOTS = 1             # Lots per leg — start with 1 for paper trading
PRODUCT        = "MIS"         # MIS = intraday auto sq-off  |  NRML = carry forward
STRIKE_OFFSET  = "ATM"         # ATM | OTM1..OTM5 | ITM1..ITM5

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 3 — TIMING  (IST 24h HH:MM)
# ═══════════════════════════════════════════════════════════════════════════════

ENTRY_TIME         = "09:30"   # Straddle entry time (fallback when USE_DTE_ENTRY_MAP=False)
EXIT_TIME          = "15:15"   # Hard square-off — closes ALL remaining open legs
MONITOR_INTERVAL_S = 15        # Seconds between P&L / SL checks

# ── DTE-aware entry time map ───────────────────────────────────────────────
#
#  Rationale: Opening IV is 15–25% inflated vs fair value for the first
#  10–15 min. Entering at 09:17 captures max premium but also captures
#  max opening volatility — 40% of all SL hits occur within 15 min of entry.
#  Waiting for IV normalisation improves fill quality and reduces false SLs.
#
#  DTE0/DTE1 (Tue/Mon): enter at 09:30 — gap settled, still captures most
#    of the day's theta on the highest-premium days.
#  DTE2 (Fri): 09:35 — moderate theta, extra 5 min improves fill price.
#  DTE3 (Thu): 09:40 — lower premium, margin-spike risk on open; cleaner
#    entry outweighs the marginal extra premium from entering earlier.
#  DTE4 (Wed): 09:45 — thin premium anyway; entering early adds risk with
#    minimal reward.
#
#  Set USE_DTE_ENTRY_MAP = False to use the fixed ENTRY_TIME for all days.
# ──────────────────────────────────────────────────────────────────────────

USE_DTE_ENTRY_MAP  = True       # True = use map below | False = use ENTRY_TIME for all

DTE_ENTRY_TIME_MAP = {
    0: "09:30",   # DTE0 = Tuesday  (expiry day)   — gap settled, max theta day
    1: "09:30",   # DTE1 = Monday   (1 day out)    — strong theta, same rationale
    2: "09:35",   # DTE2 = Friday   (2 days out)   — moderate theta, cleaner fill
    3: "09:40",   # DTE3 = Thursday (3 days out)   — lower premium, avoid open spike
    4: "09:45",   # DTE4 = Wednesday (4 days out)  — thin premium, no rush
}

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 4 — DTE FILTER  (Days To Expiry)
#
#  DTE = TRADING days from today to nearest weekly expiry.
#  Matches AlgoTest exactly — weekends are excluded.
#  NSE market holidays are handled by the OpenAlgo Python Strategy scheduler;
#  the script will not be started on a holiday, so no internal holiday check needed.
#  Source: AlgoTest DTE filter documentation.
#
#  DTE mapping for NIFTY (Tuesday expiry):
#    DTE0 = Tuesday   (expiry day)             — peak theta, max premium collapse
#    DTE1 = Monday    (1 trading day before)   — strong theta
#    DTE2 = Friday    (2 trading days before)  — moderate theta
#    DTE3 = Thursday  (3 trading days before)  — lower premium
#    DTE4 = Wednesday (4 trading days before)  — thin premium
#    DTE5 = Tuesday   (5 trading days before)  — prev week expiry day
#    DTE6 = Monday    (6 trading days before)  — prev week Monday
#
#  NOTE: DTE uses TRADING days only (Mon–Fri, excluding weekends).
#        Calendar days give WRONG numbers — Fri would be DTE4 in calendar mode
#        but is correctly DTE2 in trading-day mode (matching AlgoTest).
# ═══════════════════════════════════════════════════════════════════════════════

# TRADE_DTE = [0, 1]             # DTE0=Tue (expiry), DTE1=Mon — peak theta days
# TRADE_DTE = [0]              # Expiry day only — most aggressive theta capture
# TRADE_DTE = [0, 1, 2]       # Tue + Mon + Fri
TRADE_DTE = [0, 1, 2, 3, 4] # Full expiry week (all 5 trading days)
# TRADE_DTE = [0, 1, 2, 3, 4, 5, 6]  # All days — previous + current expiry week

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 5 — MONTH FILTER
#  November = consistent loss month across all years
#  1=Jan .. 11=Nov .. 12=Dec
# ═══════════════════════════════════════════════════════════════════════════════

SKIP_MONTHS = [11]               # Skip November
# SKIP_MONTHS = [4, 11]          # Also skip April (weaker month)
# SKIP_MONTHS = []               # Trade all months

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 6 — VIX FILTER
#  VIX < 14 : premiums too thin  |  VIX > 28 : danger zone
#  Verified against AlgoTest backtest configuration.
# ═══════════════════════════════════════════════════════════════════════════════

VIX_FILTER_ENABLED = True
VIX_MIN            = 14.0
VIX_MAX            = 28.0

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 6A — IV RANK (IVR) / IV PERCENTILE (IVP) FILTER
#
#  WHY THIS FILTER EXISTS:
#    A short straddle only has genuine statistical edge when IMPLIED VOLATILITY
#    is historically expensive — because expensive IV mean-reverts downward,
#    adding an IV-crush profit component on top of theta decay.
#    Without this filter, entries on low-IVR days face an IV headwind: even
#    if NIFTY stays flat, rising IV makes your short positions more expensive.
#
#  IV RANK (IVR):
#    IVR = (Today_VIX − 52wk_Low_VIX) / (52wk_High_VIX − 52wk_Low_VIX) × 100
#    Answers: "Where is today's IV within the past 52-week HIGH/LOW range?"
#    IVR = 0  → VIX at 52-week low   |   IVR = 100 → VIX at 52-week high
#
#  IV PERCENTILE (IVP):
#    IVP = (Days in past 252 where VIX < Today_VIX) / 252 × 100
#    Answers: "What % of past 252 days had LOWER IV than today?"
#    IVP = 75 → IV is higher than it was on 75% of trading days this year
#
#  BALANCED THRESHOLDS (recommended for NIFTY weekly straddle):
#    IVR_MIN = 30  — skip if IV is in the bottom 30% of its 52-week range
#    IVP_MIN = 40  — skip if IV is below 40th percentile of daily occurrences
#    Expected: ~20–25% fewer trades, +5–8% win-rate improvement, −30% max-DD
#
#  DATA REQUIREMENT:
#    Requires vix_history.csv — a CSV of daily VIX closing values.
#    Format (with header):
#        date,vix_close
#        2024-04-01,14.82
#        2024-04-02,15.10
#        ...
#    BOOTSTRAP: Download NIFTY VIX historical data from nseindia.com
#      → Market Data → Volatility → Historical VIX → download CSV.
#      Rename to vix_history.csv, keep only "Date" and "Close" columns,
#      rename headers to "date,vix_close", save in the same directory as this script.
#    AUTO-UPDATE: job_update_vix_history() appends today's VIX every day at
#      VIX_UPDATE_TIME automatically — file is self-maintaining after bootstrap.
#
#  IVR_FAIL_OPEN:
#    False (recommended) → if history file is missing/short, SKIP trade.
#      Rationale: unknown IV regime = unknown edge. Better to miss a trade than
#      enter blindly. This is conservative and production-safe.
#    True  → allow trade if data is unavailable (matches margin guard behaviour).
#      Use only in paper-trade / testing mode.
# ═══════════════════════════════════════════════════════════════════════════════

IVR_FILTER_ENABLED   = True
IVR_MIN              = 30.0    # Skip if IVR < 30  (IV in bottom 30% of 52wk range)
IVP_FILTER_ENABLED   = True
IVP_MIN              = 40.0    # Skip if IVP < 40% (IV below 40th percentile)
IVR_FAIL_OPEN        = False   # False = skip trade when history file unavailable
VIX_HISTORY_FILE     = "vix_history.csv"   # Path to daily VIX closing data file
VIX_HISTORY_MIN_ROWS = 100     # Minimum rows needed for a meaningful IVR/IVP calc
VIX_UPDATE_TIME      = "15:30" # IST time to auto-append today's VIX to history file

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 7 — RISK MANAGEMENT
#
#  LEG_SL_PERCENT — applied to EACH LEG INDEPENDENTLY
#    CE SL = CE_entry_price × (1 + LEG_SL_PERCENT/100)  → close CE only
#    PE SL = PE_entry_price × (1 + LEG_SL_PERCENT/100)  → close PE only
#    99.6% accuracy verified from AlgoTest CSV (2311 SL hits, median = 20.00%)
#
#  DAILY_PROFIT_TARGET_PER_LOT / DAILY_LOSS_LIMIT_PER_LOT — per lot values
#    Effective Rs. thresholds = PER_LOT value × NUMBER_OF_LOTS (auto-scaled).
#    combined = closed_leg_pnl + open_leg(s)_mtm
#    Trips → close ALL remaining open legs immediately
#
#  Example: PER_LOT target=5000, lots=2 → effective target = Rs.10,000
# ═══════════════════════════════════════════════════════════════════════════════

LEG_SL_PERCENT               = 20.0   # % of entry premium per leg  (0 = disabled)
DAILY_PROFIT_TARGET_PER_LOT  =  5000  # Rs. profit target PER LOT   (0 = disabled)
DAILY_LOSS_LIMIT_PER_LOT     = -4000  # Rs. loss limit PER LOT — NEGATIVE  (0 = disabled)

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 7A — PRE-TRADE MARGIN GUARD
#
#  Before placing the straddle, the script:
#    1. Calls client.funds()  → fetches availablecash + collateral
#    2. Calls client.margin() → fetches basket margin for CE+PE SELL MIS
#       (includes SPAN straddle portfolio offset — cheaper than two naked shorts)
#    3. Checks: (availablecash + collateral) >= required_margin × MARGIN_BUFFER
#
#  MARGIN_BUFFER = 1.20 → requires 20% headroom above margin
#    Rationale: SPAN margins can spike intraday on VIX moves.
#    Also compensates for Dhan's sequential (not basket) margin calculation.
#
#  MARGIN_GUARD_FAIL_OPEN = True  → if API fails, allow trade (don't block)
#    Set to False to be conservative: skip trade if margin API is unreachable.
#
#  ATM_STRIKE_ROUNDING = 50  → NIFTY rounds to nearest 50
#    Used to build the symbol string for the margin pre-check call.
# ═══════════════════════════════════════════════════════════════════════════════

MARGIN_GUARD_ENABLED   = True
MARGIN_BUFFER          = 1.20    # 20% safety headroom over required margin
MARGIN_GUARD_FAIL_OPEN = True    # True = allow trade if margin API fails
ATM_STRIKE_ROUNDING    = 50      # NIFTY=50, BANKNIFTY=100, FINNIFTY=50

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 7B — INTRADAY VIX SPIKE MONITOR
#
#  WHY THIS EXISTS:
#    The short straddle is short vega — a rising VIX directly increases the
#    value of both legs even if NIFTY price stays flat.  When VIX spikes
#    mid-session (surprise macro event, global sell-off, circuit trigger),
#    being short vega is the most dangerous place to be.
#    This monitor catches mid-session IV expansion and exits BEFORE the
#    combined loss exceeds what the VIX filter would have blocked at entry.
#
#  HOW IT WORKS:
#    vix_at_entry is captured during the entry filter (stored in state).
#    Every VIX_SPIKE_CHECK_INTERVAL_S seconds the monitor fetches live VIX.
#    If current_VIX > vix_at_entry × (1 + VIX_SPIKE_THRESHOLD_PCT / 100):
#      → close_all() fires immediately with reason "VIX Spike Exit"
#      → Telegram alert sent
#
#  THRESHOLD GUIDANCE (NIFTY-specific):
#    10% → very sensitive — catches even moderate IV expansion (more exits)
#    15% → balanced — catches significant intraday spikes (recommended)
#    20% → relaxed  — only exits on severe events (fewer exits, more risk)
#
#  CHECK INTERVAL:
#    VIX fetch is throttled — runs at most once per VIX_SPIKE_CHECK_INTERVAL_S
#    seconds regardless of how often the monitor tick fires.
#    300s (5 min) is sufficient for event detection without API overload.
#    Reduce to 180s (3 min) on high-volatility days if desired.
#
#  THREADING NOTE:
#    _check_vix_spike() is called from _run_monitor_tick() which already
#    holds _monitor_lock.  close_all() uses "with _monitor_lock" — this is
#    safe because _monitor_lock is a threading.RLock (reentrant), so the same
#    thread can re-acquire it without deadlocking.
# ═══════════════════════════════════════════════════════════════════════════════

VIX_SPIKE_MONITOR_ENABLED    = True
VIX_SPIKE_THRESHOLD_PCT      = 15.0   # % rise from entry VIX triggers exit
VIX_SPIKE_CHECK_INTERVAL_S   = 300    # Seconds between VIX spike checks (5 min)

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 8 — EXPIRY
#  Format : DDMMMYY uppercase  e.g. "25MAR26"
#  AUTO_EXPIRY = True resolves the nearest Tuesday automatically every day.
#  Set MANUAL_EXPIRY only when AUTO_EXPIRY = False.
# ═══════════════════════════════════════════════════════════════════════════════

AUTO_EXPIRY   = True           # True = auto nearest Tuesday (NIFTY weekly expiry day)
MANUAL_EXPIRY = "25MAR26"      # Used only when AUTO_EXPIRY = False  (must be a Tuesday)

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 9 — STRATEGY NAME
#  Must match exactly what is registered in the OpenAlgo dashboard
# ═══════════════════════════════════════════════════════════════════════════════

STRATEGY_NAME = "Nifty Trending Straddle"

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 10 — TELEGRAM ALERTS
#  BOT_TOKEN from @BotFather  |  CHAT_ID from @userinfobot
# ═══════════════════════════════════════════════════════════════════════════════

TELEGRAM_ENABLED   = True
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 11 — STATE FILE
#  JSON persisted atomically after every state mutation.
#  Use absolute path when running as a systemd service or crontab.
#  e.g.  STATE_FILE = "/home/ubuntu/strategy/strategy_state.json"
# ═══════════════════════════════════════════════════════════════════════════════

STATE_FILE = "strategy_state.json"

# ═══════════════════════════════════════════════════════════════════════════════
#  END OF CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════


# ───────────────────────────────────────────────────────────────────────────────
#  INTERNAL CONSTANTS
# ───────────────────────────────────────────────────────────────────────────────

VERSION     = "5.2.0"
IST         = pytz.timezone("Asia/Kolkata")
OPTION_EXCH = "NFO"        # All F&O option contracts (quotes / positions)
INDEX_EXCH  = "NSE_INDEX"  # Underlying index + VIX (order entry)
VIX_SYMBOL  = "INDIAVIX"   # docs.openalgo.in/symbol-format

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTH_NAMES = {
    1: "January",   2: "February", 3: "March",    4: "April",
    5: "May",       6: "June",     7: "July",      8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}

# Monitor job + state mutation guard (RLock — reentrant so close_all can call
# close_one_leg while both hold the lock within the same call chain).
_monitor_lock = threading.RLock()

# Timestamp of the last intraday VIX spike check — module-level so it persists
# across monitor ticks but resets on each new Python process (correct behaviour:
# we want a fresh check timer for every trading session, not persisted to disk).
_last_vix_spike_check_time = None

# ── Effective daily target / limit (auto-scaled with NUMBER_OF_LOTS) ──────────
#  DO NOT edit these — change DAILY_PROFIT_TARGET_PER_LOT / DAILY_LOSS_LIMIT_PER_LOT
#  in Section 7 above. Computed once at startup.
#
#  Examples:
#    1 lot: target = 5000×1 = Rs. 5,000  |  limit = -4000×1 = Rs. -4,000
#    2 lots: target = 5000×2 = Rs.10,000  |  limit = -4000×2 = Rs. -8,000
#    3 lots: target = 5000×3 = Rs.15,000  |  limit = -4000×3 = Rs.-12,000
DAILY_PROFIT_TARGET = DAILY_PROFIT_TARGET_PER_LOT * NUMBER_OF_LOTS
DAILY_LOSS_LIMIT    = DAILY_LOSS_LIMIT_PER_LOT    * NUMBER_OF_LOTS


# ═══════════════════════════════════════════════════════════════════════════════
#  STATE
#
#  PARTIAL SQUARE OFF requires independent per-leg tracking:
#
#  ce_active / pe_active
#    True  = leg is still OPEN (position exists at broker)
#    False = leg has been CLOSED (SL fired, target/limit hit, or hard exit)
#
#  in_position
#    True  = AT LEAST ONE leg is still active
#    False = BOTH legs are closed (fully flat)
#
#  closed_pnl
#    Running sum of approximate REALISED P&L from legs that have been closed.
#    When CE SL fires and CE closes → CE approx P&L added to closed_pnl.
#    PE still open → its live MTM is called "open_mtm".
#    combined_pnl = closed_pnl + open_mtm   (used for target/limit checks)
#
#  entry_price_ce / entry_price_pe
#    Average fill prices fetched via orderstatus() after entry.
#    Used to compute per-leg SL levels (FIXED at entry, never change):
#      CE SL level = entry_price_ce × (1 + LEG_SL_PERCENT/100)
#      PE SL level = entry_price_pe × (1 + LEG_SL_PERCENT/100)
#
#  margin_required / margin_available
#    Captured at entry time from margin guard check.
#    Stored for logging and Telegram only — not used in trade logic.
# ═══════════════════════════════════════════════════════════════════════════════

state = {
    # ── Position flags ────────────────────────────────────────────────────────
    "in_position"      : False,   # True if ANY leg is still open
    "ce_active"        : False,   # True = CE leg open at broker
    "pe_active"        : False,   # True = PE leg open at broker

    # ── Leg symbols (resolved by OpenAlgo from ATM + expiry) ─────────────────
    "symbol_ce"        : "",      # e.g. NIFTY25MAR2623000CE
    "symbol_pe"        : "",      # e.g. NIFTY25MAR2623000PE

    # ── Order IDs ─────────────────────────────────────────────────────────────
    "orderid_ce"       : "",
    "orderid_pe"       : "",

    # ── Entry fill prices — basis of per-leg SL calculation ──────────────────
    "entry_price_ce"   : 0.0,
    "entry_price_pe"   : 0.0,

    # ── Realised P&L from legs already closed this session ───────────────────
    "closed_pnl"       : 0.0,

    # ── Context at entry ─────────────────────────────────────────────────────
    "underlying_ltp"   : 0.0,
    "vix_at_entry"     : 0.0,
    "ivr_at_entry"     : 0.0,     # IV Rank at entry time (0–100)
    "ivp_at_entry"     : 0.0,     # IV Percentile at entry time (0–100)
    "entry_time"       : None,    # ISO string (JSON-serialisable)
    "entry_date"       : None,    # YYYY-MM-DD (stale-state detection on restart)

    # ── Margin info captured at entry ─────────────────────────────────────────
    "margin_required"  : 0.0,     # From margin guard check
    "margin_available" : 0.0,     # From funds check (cash + collateral)

    # ── Running P&L (updated every monitor cycle) ────────────────────────────
    "today_pnl"        : 0.0,     # closed_pnl + current open_mtm

    # ── Session stats ─────────────────────────────────────────────────────────
    "trade_count"      : 0,
    "exit_reason"      : "",
}


# ───────────────────────────────────────────────────────────────────────────────
#  OpenAlgo SDK client
# ───────────────────────────────────────────────────────────────────────────────

client = OpenAlgoClient(api_key=OPENALGO_API_KEY, host=OPENALGO_HOST)


# ═══════════════════════════════════════════════════════════════════════════════
#  LOGGER
#  All output via print(flush=True) — captured by OpenAlgo's log system.
#  Format: [YYYY-MM-DD HH:MM:SS IST] [LEVEL   ] message
# ═══════════════════════════════════════════════════════════════════════════════

def now_ist() -> datetime:
    return datetime.now(IST)

def ts() -> str:
    return f"[{now_ist().strftime('%Y-%m-%d %H:%M:%S')} IST]"

def plog(level: str, msg: str):
    print(f"{ts()} [{level:<8}] {msg}", flush=True)

def pinfo(msg: str):  plog("INFO",    msg)
def pwarn(msg: str):  plog("WARNING", msg)
def perr(msg: str):   plog("ERROR",   msg)
def pdebug(msg: str): plog("DEBUG",   msg)

def psep():
    print(f"{ts()} {'─' * 68}", flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  STATE PERSISTENCE  (atomic write — crash-safe)
#
#  Uses write-to-temp-then-rename pattern.
#  On Linux/Mac: rename() is atomic — partial writes never corrupt STATE_FILE.
#  On Windows: uses os.replace() which is atomic on NTFS.
# ═══════════════════════════════════════════════════════════════════════════════

def save_state():
    """
    Atomically write current in-memory state to STATE_FILE (JSON).
    Called after EVERY state mutation — ensures restart safety at any moment.
    Uses temp-file + rename for crash-safe atomic writes.
    """
    try:
        payload = dict(state)
        # Serialize datetime objects to ISO strings for JSON
        if isinstance(payload.get("entry_time"), datetime):
            payload["entry_time"] = payload["entry_time"].isoformat()

        state_dir    = os.path.dirname(os.path.abspath(STATE_FILE)) or "."
        fd, tmp_path = tempfile.mkstemp(dir=state_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, indent=2)
            # Atomic rename: replaces STATE_FILE if it exists
            os.replace(tmp_path, STATE_FILE)
        except Exception:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        pdebug(f"State saved atomically → {STATE_FILE}")

    except Exception as exc:
        pwarn(f"State save failed: {exc}")


def load_state() -> dict:
    """
    Load saved state from STATE_FILE.
    Returns loaded dict, or empty dict if file missing/corrupt.
    """
    if not os.path.exists(STATE_FILE):
        pinfo(f"No state file at {STATE_FILE} — fresh start")
        return {}
    try:
        with open(STATE_FILE) as f:
            loaded = json.load(f)
        pinfo(f"State file loaded: {STATE_FILE}")
        return loaded
    except Exception as exc:
        pwarn(f"State file corrupt ({exc}) — fresh start")
        return {}


def clear_state_file():
    """
    Delete STATE_FILE.
    Only called after position is FULLY FLAT (both legs confirmed closed).
    """
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            pinfo(f"State file cleared: {STATE_FILE}")
    except Exception as exc:
        pwarn(f"State file remove failed: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def qty() -> int:
    """Total quantity per leg = NUMBER_OF_LOTS × LOT_SIZE."""
    return NUMBER_OF_LOTS * LOT_SIZE

def parse_hhmm(t: str):
    h, m = t.strip().split(":")
    return int(h), int(m)

def active_legs() -> list:
    """
    Return list of currently open leg identifiers.
    Possible: ['CE', 'PE'] | ['CE'] | ['PE'] | []
    """
    result = []
    if state["ce_active"]: result.append("CE")
    if state["pe_active"]: result.append("PE")
    return result

def sl_level(leg: str) -> float:
    """
    Compute SL trigger price for a given leg.
    Returns entry_price × (1 + LEG_SL_PERCENT/100).
    Returns 0.0 if entry price was not captured (SL check will be skipped).
    """
    entry = state[f"entry_price_{leg.lower()}"]
    if entry <= 0:
        return 0.0
    return round(entry * (1.0 + LEG_SL_PERCENT / 100.0), 2)


# ═══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════════════════════

def telegram(msg: str):
    """
    Send Telegram message. Never raises — failures logged as warnings.
    Silently skips if disabled or credentials missing.
    """
    if not TELEGRAM_ENABLED or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id"    : TELEGRAM_CHAT_ID,
            "text"       : f"[{STRATEGY_NAME} v{VERSION}]\n{msg}",
            "parse_mode" : "HTML",
        }
        r = requests.post(url, json=data, timeout=6)
        if r.status_code != 200:
            pwarn(f"Telegram HTTP {r.status_code}: {r.text[:120]}")
    except Exception as exc:
        pwarn(f"Telegram failed: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
#  EXPIRY CALCULATION  (DDMMMYY uppercase per OpenAlgo docs)
# ═══════════════════════════════════════════════════════════════════════════════

def _nearest_tuesday_date() -> date:
    """
    Compute nearest NIFTY weekly expiry date (Tuesday) silently — no logging.
    Called from get_dte() and _print_banner() to avoid spam.

    NSE EXPIRY: NIFTY 50 weekly & monthly options expire every TUESDAY
    (effective September 2, 2025 per SEBI F&O restructuring).

    Logic:
      • If today IS Tuesday and time < 15:30 IST → use today
      • If today IS Tuesday and time >= 15:30 IST → use next Tuesday
      • Any other day → find next upcoming Tuesday

    NOTE: Uses now_ist().date() (not date.today()) so the script works
    correctly when the server is in UTC or any non-IST timezone.
    """
    now        = now_ist()
    today      = now.date()           # IST date — not OS date (critical fix)
    days_ahead = (1 - today.weekday()) % 7   # 0 if today is already Tuesday

    if days_ahead == 0 and (now.hour, now.minute) >= (15, 30):
        days_ahead = 7

    return today + timedelta(days=days_ahead)


def nearest_tuesday_expiry() -> str:
    """
    Return nearest NIFTY weekly expiry (TUESDAY) as DDMMMYY, with logging.
    Called only in get_expiry() — once per entry flow.
    """
    expiry = _nearest_tuesday_date()
    result = expiry.strftime("%d%b%y").upper()
    pinfo(f"Auto expiry: {result}  (date: {expiry}, {expiry.strftime('%A')})")
    return result


def get_expiry() -> str:
    """Return the active expiry string based on AUTO_EXPIRY setting."""
    if AUTO_EXPIRY:
        return nearest_tuesday_expiry()
    pinfo(f"Manual expiry: {MANUAL_EXPIRY}")
    return MANUAL_EXPIRY


def _get_expiry_date_silent() -> date:
    """
    Return expiry as a date object WITHOUT logging. Used by get_dte() and
    _print_banner() to avoid spurious log lines during monitor ticks or startup.
    """
    if AUTO_EXPIRY:
        return _nearest_tuesday_date()
    # Parse MANUAL_EXPIRY string to date
    try:
        return datetime.strptime(MANUAL_EXPIRY, "%d%b%y").date()
    except Exception:
        return _nearest_tuesday_date()


# ═══════════════════════════════════════════════════════════════════════════════
#  DTE CALCULATION  (trading days — matches AlgoTest exactly)
#
#  AlgoTest definition:
#    "DTE only includes trading days and doesn't include weekends and
#     market holidays. If the expiry is on Tuesday then Monday = DTE1
#     and Friday = DTE2."
#
#  We count Mon–Fri only between today and expiry, excluding weekends.
#  NSE market holidays are handled upstream by the OpenAlgo Python Strategy
#  scheduler — the script is never started on a market holiday, so no internal
#  holiday calendar is needed here.
#
#  FIX-1 (v5.0.0): get_dte() now uses _get_expiry_date_silent() instead of
#  get_expiry(), which previously logged "Auto expiry: …" on every 15s tick.
# ═══════════════════════════════════════════════════════════════════════════════

def get_dte() -> int:
    """
    Compute DTE (Days To Expiry) = TRADING days from today to nearest expiry.
    Counts Mon–Fri only, skipping Sat and Sun. Matches AlgoTest's DTE filter.
    Does NOT log expiry resolution — only logs the resulting DTE.

    NIFTY Tuesday expiry mapping:
      DTE0 = Tuesday  (expiry day)
      DTE1 = Monday
      DTE2 = Friday   (previous week)
      DTE3 = Thursday (previous week)
      DTE4 = Wednesday(previous week)
      DTE5 = Tuesday  (previous week — that week's expiry day)
      DTE6 = Monday   (previous week)

    Uses now_ist().date() (not date.today()) to be timezone-safe.
    Skips Sat/Sun only. NSE market holidays are handled by the OpenAlgo scheduler.
    """
    today       = now_ist().date()    # IST date — not OS date (critical fix)
    expiry_date = _get_expiry_date_silent()

    # Count Mon–Fri days between today (exclusive) and expiry (inclusive)
    dte     = 0
    current = today
    while current < expiry_date:
        current += timedelta(days=1)
        if current.weekday() < 5:
            dte += 1

    pdebug(
        f"DTE: {dte}  "
        f"(today: {today} {DAY_NAMES[today.weekday()]}  "
        f"expiry: {expiry_date.strftime('%d%b%y').upper()} {expiry_date.strftime('%A')})"
    )
    return dte


# ═══════════════════════════════════════════════════════════════════════════════
#  VIX FETCH  (primary: OpenAlgo  |  fallback: NSE direct API)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_vix() -> float:
    """
    Fetch India VIX LTP. Returns float > 0, or -1.0 on total failure.

    Primary: OpenAlgo SDK quotes()
    Fallback: NSE direct API (with proper cookie pre-auth)
    """
    # Primary: OpenAlgo SDK
    try:
        resp = client.quotes(symbol=VIX_SYMBOL, exchange=INDEX_EXCH)
        if isinstance(resp, dict) and resp.get("status") == "success":
            ltp = float(resp.get("data", {}).get("ltp", -1))
            if ltp > 0:
                pinfo(f"India VIX (OpenAlgo): {ltp:.2f}")
                return ltp
    except Exception as exc:
        pwarn(f"OpenAlgo VIX exception: {exc}")

    # Fallback: NSE direct API (FIX-9: proper session + cookie pre-auth)
    try:
        hdrs = {
            "User-Agent"      : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept"          : "application/json, text/plain, */*",
            "Accept-Language" : "en-US,en;q=0.9",
            "Referer"         : "https://www.nseindia.com/",
        }
        sess = requests.Session()
        # NSE requires cookie grab before API calls
        sess.get("https://www.nseindia.com", headers=hdrs, timeout=8)
        sess.get("https://www.nseindia.com/option-chain", headers=hdrs, timeout=8)
        r = sess.get("https://www.nseindia.com/api/allIndices", headers=hdrs, timeout=8)
        r.raise_for_status()
        for item in r.json().get("data", []):
            if item.get("index", "").replace(" ", "").upper() == "INDIAVIX":
                vix = float(item["last"])
                pinfo(f"India VIX (NSE fallback): {vix:.2f}")
                return vix
    except Exception as exc:
        pwarn(f"NSE VIX fallback exception: {exc}")

    perr("India VIX unavailable from all sources")
    return -1.0


def vix_ok() -> bool:
    """Check VIX filter. Returns True = OK to trade, False = skip."""
    if not VIX_FILTER_ENABLED:
        pinfo("VIX filter disabled")
        return True

    vix = fetch_vix()

    if vix < 0:
        pwarn("VIX fetch failed — skipping trade (precaution)")
        telegram("VIX unavailable — no trade today (precaution)")
        return False
    if vix < VIX_MIN:
        pwarn(f"VIX {vix:.2f} < {VIX_MIN} — premiums too thin, skipping")
        telegram(f"VIX {vix:.2f} &lt; {VIX_MIN} — thin premiums, no trade today")
        return False
    if vix > VIX_MAX:
        pwarn(f"VIX {vix:.2f} > {VIX_MAX} — danger zone, skipping")
        telegram(f"VIX {vix:.2f} &gt; {VIX_MAX} — DANGER ZONE, no trade today!")
        return False

    pinfo(f"VIX {vix:.2f} within [{VIX_MIN}–{VIX_MAX}] — OK to trade")
    state["vix_at_entry"] = vix
    return True


# ═══════════════════════════════════════════════════════════════════════════════
#  VIX HISTORY  —  DAILY DATA MANAGEMENT
#
#  VIX_HISTORY_FILE is a local CSV maintained automatically by this script.
#  It is read at entry time to compute IVR and IVP.
#  It is written once per trading day at VIX_UPDATE_TIME by job_update_vix_history().
#
#  File format (with header row):
#    date,vix_close
#    2024-04-01,14.82
#    2024-04-02,15.10
#    ...
#
#  BOOTSTRAP (first-time setup):
#    1. Download NSE VIX historical data from nseindia.com
#       → Market Data → Volatility → Historical VIX
#    2. Keep only "Date" and "Close" columns, rename to "date,vix_close"
#    3. Save as vix_history.csv in the same directory as this script
#    After bootstrap the file self-maintains via the 15:30 daily update job.
# ═══════════════════════════════════════════════════════════════════════════════

def _load_vix_history_raw() -> list:
    """
    Load all rows from VIX_HISTORY_FILE.

    Returns a list of (date_str, vix_float) tuples sorted chronologically.
    Returns [] if the file does not exist, is empty, or cannot be parsed.
    Malformed individual rows are skipped — the rest of the file is still used.
    """
    if not os.path.exists(VIX_HISTORY_FILE):
        return []
    try:
        rows = []
        with open(VIX_HISTORY_FILE) as f:
            for line in f:
                line = line.strip()
                # Skip blank lines and the header row
                if not line or line.lower().startswith("date"):
                    continue
                parts = line.split(",")
                if len(parts) < 2:
                    continue
                try:
                    date_str = parts[0].strip()
                    vix_val  = float(parts[1].strip())
                    # Basic sanity: VIX should be a positive number in a sane range
                    if vix_val > 0:
                        rows.append((date_str, vix_val))
                except (ValueError, IndexError):
                    continue   # Skip malformed rows silently
        # ISO date strings sort lexicographically = chronologically — no datetime parse needed
        rows.sort(key=lambda x: x[0])
        return rows
    except Exception as exc:
        pwarn(f"VIX history raw load failed: {exc}")
        return []


def _load_vix_history() -> list:
    """
    Load VIX history values for IVR/IVP calculation.

    Returns the last 252 closing VIX values as a list of floats (oldest first).
    Returns None if the file has fewer than VIX_HISTORY_MIN_ROWS valid rows,
    signalling that the data is insufficient for a meaningful IVR/IVP result.
    """
    rows = _load_vix_history_raw()
    n    = len(rows)

    if n < VIX_HISTORY_MIN_ROWS:
        pwarn(
            f"VIX history: {n} rows found — need at least {VIX_HISTORY_MIN_ROWS} "
            f"for IVR/IVP calculation. "
            f"{'Bootstrap the file from NSE historical data.' if n == 0 else 'Continuing to collect daily data.'}"
        )
        return None

    # Use the most recent 252 rows (one trading year) for the 52-week window.
    # If fewer than 252 rows exist but >= VIX_HISTORY_MIN_ROWS, use all available
    # rows — the calculation is still meaningful, just covers a shorter window.
    recent = rows[-252:]
    return [v for _, v in recent]


def compute_ivr(current_vix: float, history_values: list) -> float:
    """
    Compute IV Rank (IVR) as a percentage (0–100).

    IVR = (current_vix − min) / (max − min) × 100

    Edge case: if max == min (flat VIX history — extremely rare), return 50.0
    so the filter neither blocks nor artificially passes on degenerate data.

    Parameters
    ----------
    current_vix    : today's VIX value
    history_values : list of float VIX closes (typically 252 values)
    """
    low  = min(history_values)
    high = max(history_values)
    if high == low:
        pwarn(f"compute_ivr: 52-week high == low ({high:.2f}) — returning neutral 50.0")
        return 50.0
    ivr = (current_vix - low) / (high - low) * 100.0
    # Clamp to [0, 100] — current VIX could theoretically be outside the
    # historical window if today is a new extreme (rare but possible)
    return round(max(0.0, min(100.0, ivr)), 1)


def compute_ivp(current_vix: float, history_values: list) -> float:
    """
    Compute IV Percentile (IVP) as a percentage (0–100).

    IVP = count(days where vix_close < current_vix) / len(history) × 100

    Uses strict less-than (<), not <=, which is the standard definition.
    Days equal to current_vix are not counted as "below".

    Parameters
    ----------
    current_vix    : today's VIX value
    history_values : list of float VIX closes (typically 252 values)
    """
    if not history_values:
        return 50.0
    days_below = sum(1 for v in history_values if v < current_vix)
    ivp = days_below / len(history_values) * 100.0
    return round(ivp, 1)


def ivr_ivp_ok(current_vix: float) -> bool:
    """
    IVR / IVP filter gate — called from job_entry() after vix_ok() passes.

    Receives current_vix already fetched by vix_ok() — no duplicate API call.

    Logic:
      1. If both filters disabled → return True immediately (no-op)
      2. Load 252-day VIX history from VIX_HISTORY_FILE
         → If unavailable: apply IVR_FAIL_OPEN policy (fail-closed by default)
      3. Compute IVR and IVP (always compute both for logging/analytics even if
         one filter is disabled — values stored in state regardless)
      4. IVR check (if IVR_FILTER_ENABLED): fail if ivr < IVR_MIN
      5. IVP check (if IVP_FILTER_ENABLED): fail if ivp < IVP_MIN
      6. Store ivr_at_entry / ivp_at_entry in state for Telegram + analytics

    Returns True (proceed to entry) or False (skip today's trade).
    """
    both_disabled = not IVR_FILTER_ENABLED and not IVP_FILTER_ENABLED
    if both_disabled:
        pinfo("IVR/IVP filter: both disabled — skipping check")
        return True

    psep()
    pinfo("IVR/IVP FILTER CHECK")
    pinfo(f"  Current VIX : {current_vix:.2f}")

    # ── Load VIX history ──────────────────────────────────────────────────────
    history_values = _load_vix_history()

    if history_values is None:
        # Insufficient or missing data — apply fail-open/closed policy
        if IVR_FAIL_OPEN:
            pwarn(
                "IVR/IVP: VIX history insufficient — fail-open policy, "
                "proceeding with entry. Bootstrap vix_history.csv for full protection."
            )
            telegram(
                "⚠️ IVR/IVP filter: VIX history insufficient\n"
                "Proceeding (fail-open). Bootstrap vix_history.csv."
            )
            psep()
            return True
        else:
            pwarn(
                "IVR/IVP: VIX history insufficient — fail-closed policy, "
                "skipping trade. Bootstrap vix_history.csv to enable this filter."
            )
            telegram(
                "IVR/IVP filter: VIX history insufficient — trade SKIPPED (fail-closed).\n"
                "Bootstrap vix_history.csv from NSE historical VIX data."
            )
            psep()
            return False

    n    = len(history_values)
    low  = min(history_values)
    high = max(history_values)

    # ── Compute both metrics (always — for logging and state storage) ─────────
    ivr = compute_ivr(current_vix, history_values)
    ivp = compute_ivp(current_vix, history_values)

    pinfo(f"  History     : {n} days  |  52wk Low: {low:.2f}  52wk High: {high:.2f}")
    pinfo(f"  IVR         : {ivr:.1f}  (threshold: >= {IVR_MIN}  |  enabled: {IVR_FILTER_ENABLED})")
    pinfo(f"  IVP         : {ivp:.1f}%  (threshold: >= {IVP_MIN}%  |  enabled: {IVP_FILTER_ENABLED})")

    # ── IVR check ─────────────────────────────────────────────────────────────
    if IVR_FILTER_ENABLED:
        if ivr < IVR_MIN:
            pwarn(
                f"  IVR CHECK: FAIL ✗  "
                f"IVR {ivr:.1f} < {IVR_MIN} — "
                f"IV in bottom {ivr:.0f}% of its 52-week range, not rich enough to sell"
            )
            psep()
            telegram(
                f"IVR filter: SKIP today\n"
                f"IVR {ivr:.1f} &lt; {IVR_MIN} — IV not historically rich\n"
                f"VIX: {current_vix:.2f}  |  52wk range: {low:.2f}–{high:.2f}"
            )
            return False
        pinfo(f"  IVR CHECK: PASS ✓  IVR {ivr:.1f} >= {IVR_MIN}")

    # ── IVP check ─────────────────────────────────────────────────────────────
    if IVP_FILTER_ENABLED:
        if ivp < IVP_MIN:
            pwarn(
                f"  IVP CHECK: FAIL ✗  "
                f"IVP {ivp:.1f}% < {IVP_MIN}% — "
                f"VIX is below {ivp:.0f}% of the past {n} trading days, below median"
            )
            psep()
            telegram(
                f"IVP filter: SKIP today\n"
                f"IVP {ivp:.1f}% &lt; {IVP_MIN}% — IV below historical median\n"
                f"VIX: {current_vix:.2f}  |  Days below today's VIX: {int(ivp * n / 100)}/{n}"
            )
            return False
        pinfo(f"  IVP CHECK: PASS ✓  IVP {ivp:.1f}% >= {IVP_MIN}%")

    # ── Both checks passed — store in state for Telegram + analytics ──────────
    state["ivr_at_entry"] = ivr
    state["ivp_at_entry"] = ivp

    pinfo(
        f"  IVR/IVP filter: PASS ✓ — "
        f"IV is historically rich — IV-crush tailwind expected"
    )
    psep()
    return True


def _check_vix_history_on_startup():
    """
    Validate VIX history file at startup and log actionable status.

    Checks:
      1. File exists
      2. Row count (>= VIX_HISTORY_MIN_ROWS)
      3. Staleness (last recorded date vs today)

    Does NOT block startup — only logs warnings.
    Actual trade decisions use IVR_FAIL_OPEN to determine behaviour when data
    is insufficient.  This function is purely informational.
    """
    if not IVR_FILTER_ENABLED and not IVP_FILTER_ENABLED:
        pinfo("IVR/IVP filter disabled — skipping VIX history startup check")
        return

    psep()
    pinfo("VIX HISTORY STARTUP CHECK")

    if not os.path.exists(VIX_HISTORY_FILE):
        pwarn(f"  VIX history file NOT FOUND: {os.path.abspath(VIX_HISTORY_FILE)}")
        pwarn("  IVR/IVP filter will SKIP trades (fail-closed) until file is created.")
        pwarn("  Bootstrap: download NSE historical VIX → save as vix_history.csv")
        pwarn("  Format: header 'date,vix_close' then rows like '2025-01-02,14.82'")
        psep()
        return

    rows = _load_vix_history_raw()
    n    = len(rows)

    if n == 0:
        pwarn(f"  VIX history file EXISTS but has 0 valid rows: {VIX_HISTORY_FILE}")
        pwarn("  Check file format: header must be 'date,vix_close', values must be numeric")
        psep()
        return

    latest_date_str = rows[-1][0]
    latest_vix      = rows[-1][1]

    pinfo(f"  File        : {os.path.abspath(VIX_HISTORY_FILE)}")
    pinfo(f"  Rows        : {n}  (need >= {VIX_HISTORY_MIN_ROWS} for full accuracy)")
    pinfo(f"  Latest entry: {latest_date_str}  VIX {latest_vix:.2f}")

    if n < VIX_HISTORY_MIN_ROWS:
        pwarn(
            f"  Row count {n} < {VIX_HISTORY_MIN_ROWS} minimum. "
            f"IVR/IVP accuracy is limited. "
            f"{'Add more history from NSE data.' if n < 50 else 'Growing — will improve over time.'}"
        )

    # Staleness check — warn if last entry is more than 5 calendar days ago
    try:
        latest_dt = date.fromisoformat(latest_date_str)
        today     = now_ist().date()
        days_old  = (today - latest_dt).days
        if days_old > 5:
            pwarn(
                f"  ⚠ VIX history is {days_old} calendar days stale "
                f"(last: {latest_date_str}). "
                f"The 15:30 auto-update job will fix this today."
            )
        else:
            pinfo(f"  Freshness   : {days_old} calendar day(s) old — OK")
    except (ValueError, TypeError):
        pwarn(f"  Could not parse latest date: {latest_date_str!r}")

    psep()


# ═══════════════════════════════════════════════════════════════════════════════
#  DTE + MONTH FILTERS
# ═══════════════════════════════════════════════════════════════════════════════

def dte_filter_ok() -> bool:
    """
    Return True if today passes the weekend check, the month filter, and the
    DTE filter — in that order (cheapest checks first).

    Weekend guard:
      Saturday and Sunday are never valid trading days. AlgoTest's DTE filter
      only applies to Mon–Fri; weekends do not appear in backtest data at all.
      Without this guard, Friday/Saturday/Sunday all compute the same DTE value
      because the trading-day loop counts only Mon–Fri between today and expiry:
        Friday  Mar 13 → Mon(1)+Tue(2) = DTE2  ← correct trading day
        Saturday Mar 14 → Mon(1)+Tue(2) = DTE2  ← non-trading, same DTE value!
        Sunday  Mar 15 → Mon(1)+Tue(2) = DTE2  ← non-trading, same DTE value!
      The APScheduler (day_of_week="mon-fri") already prevents automatic trades
      on weekends. This guard makes manual_entry() on weekends reject cleanly.

    Month filter:
      Skip if current month is in SKIP_MONTHS (no API call needed).

    DTE filter:
      Compute today's trading-day DTE (AlgoTest-compatible).
      Allow trade only if DTE is in TRADE_DTE list.
    """
    now     = now_ist()
    weekday = now.weekday()   # 0=Mon … 4=Fri  5=Sat  6=Sun
    month   = now.month

    # ── Weekend guard — must be first ─────────────────────────────────────────
    if weekday >= 5:
        pinfo(
            f"{DAY_NAMES[weekday]} is not a trading day — skipping "
            f"(scheduler never fires on weekends; this path only reached via manual_entry)"
        )
        return False

    # ── Month filter ──────────────────────────────────────────────────────────
    if month in SKIP_MONTHS:
        pinfo(f"{MONTH_NAMES[month]} is in SKIP_MONTHS — skipping")
        telegram(f"Skipping — {MONTH_NAMES[month]} is a configured skip month")
        return False

    # ── DTE filter ────────────────────────────────────────────────────────────
    dte = get_dte()

    if dte not in TRADE_DTE:
        pinfo(
            f"DTE{dte} ({DAY_NAMES[weekday]}) not in "
            f"TRADE_DTE {['DTE' + str(d) for d in sorted(TRADE_DTE)]} — skipping"
        )
        return False

    pinfo(
        f"DTE filter OK: DTE{dte} ({DAY_NAMES[weekday]}) "
        f"| month: {MONTH_NAMES[month]} {now.year}"
    )
    return True


# ═══════════════════════════════════════════════════════════════════════════════
#  PRE-TRADE MARGIN GUARD
#
#  OpenAlgo APIs used:
#    /api/v1/funds  → availablecash + collateral (pledged securities, haircut applied)
#    /api/v1/margin → basket margin for SELL CE + SELL PE MIS
#                     returns total_margin_required with SPAN straddle offset
# ═══════════════════════════════════════════════════════════════════════════════

def _get_atm_strike_for_margin() -> str:
    """
    Fetch NIFTY spot LTP and return nearest ATM strike as string.
    Used only for the margin pre-check symbol string — not for order entry.
    OpenAlgo resolves the actual ATM strike automatically at order time.
    Falls back to a hardcoded reasonable default on any failure.
    """
    try:
        q = client.quotes(symbol=UNDERLYING, exchange=INDEX_EXCH)
        if isinstance(q, dict) and q.get("status") == "success":
            ltp = float(q.get("data", {}).get("ltp", 0))
            if ltp > 0:
                atm = round(ltp / ATM_STRIKE_ROUNDING) * ATM_STRIKE_ROUNDING
                pdebug(f"ATM strike for margin check: {atm}  (LTP: {ltp:.2f})")
                return str(int(atm))
    except Exception as exc:
        pwarn(f"ATM LTP fetch failed: {exc}")
    # Safe fallback — margin estimate may be slightly off but won't block trade
    return "23000"


def check_margin_sufficient(expiry: str) -> bool:
    """
    Pre-trade margin guard. Runs before optionsmultiorder().

    Step 1: GET available capital via client.funds()
    Step 2: GET basket margin via client.margin() for CE+PE SELL MIS
    Step 3: Sufficiency check — total_available >= required_margin × MARGIN_BUFFER

    Returns True (proceed) or False (skip trade).
    """
    if not MARGIN_GUARD_ENABLED:
        pinfo("Margin guard disabled — skipping pre-trade margin check")
        return True

    psep()
    pinfo("PRE-TRADE MARGIN CHECK")

    # ── Step 1: Fetch available capital (cash + collateral) ──────────────────
    available_cash = 0.0
    collateral     = 0.0
    utilised       = 0.0

    try:
        funds_resp = client.funds()
        if isinstance(funds_resp, dict) and funds_resp.get("status") == "success":
            data           = funds_resp.get("data", {})
            available_cash = float(data.get("availablecash",  0) or 0)
            collateral     = float(data.get("collateral",     0) or 0)
            utilised       = float(data.get("utiliseddebits", 0) or 0)
            pinfo(f"  Available cash  : Rs.{available_cash:,.2f}")
            pinfo(f"  Collateral      : Rs.{collateral:,.2f}  (pledged securities)")
            pinfo(f"  Utilised debits : Rs.{utilised:,.2f}  (existing margin)")
        else:
            msg = funds_resp.get("message", "") if isinstance(funds_resp, dict) else str(funds_resp)
            pwarn(f"funds() failed: {msg}")
            if MARGIN_GUARD_FAIL_OPEN:
                pwarn("Margin guard fail-open: proceeding with entry despite funds() failure")
                return True
            else:
                pwarn("Margin guard fail-closed: skipping trade due to funds() failure")
                telegram("Margin guard: funds() API failed — trade SKIPPED (fail-closed mode)")
                return False
    except Exception as exc:
        pwarn(f"funds() exception: {exc}")
        if MARGIN_GUARD_FAIL_OPEN:
            pwarn("Margin guard fail-open: proceeding with entry despite exception")
            return True
        telegram(f"Margin guard: funds() exception — trade SKIPPED\n{exc}")
        return False

    total_available = available_cash + collateral
    pinfo(f"  Total available : Rs.{total_available:,.2f}  (cash + collateral)")

    # ── Step 2: Fetch basket margin for straddle ─────────────────────────────
    required_margin = 0.0
    span_margin     = 0.0
    exposure_margin = 0.0
    atm_strike      = _get_atm_strike_for_margin()

    ce_symbol = f"{UNDERLYING}{expiry}{atm_strike}CE"
    pe_symbol = f"{UNDERLYING}{expiry}{atm_strike}PE"

    pinfo(f"  Margin check symbols: {ce_symbol} + {pe_symbol}")
    pinfo(f"  Qty/leg: {qty()}  |  Product: {PRODUCT}")

    try:
        margin_resp = client.margin(
            positions=[
                {
                    "symbol"    : ce_symbol,
                    "exchange"  : OPTION_EXCH,
                    "action"    : "SELL",
                    "product"   : PRODUCT,
                    "pricetype" : "MARKET",
                    "quantity"  : str(qty()),
                    "price"     : "0",
                },
                {
                    "symbol"    : pe_symbol,
                    "exchange"  : OPTION_EXCH,
                    "action"    : "SELL",
                    "product"   : PRODUCT,
                    "pricetype" : "MARKET",
                    "quantity"  : str(qty()),
                    "price"     : "0",
                },
            ]
        )

        if isinstance(margin_resp, dict) and margin_resp.get("status") == "success":
            margin_data     = margin_resp.get("data", {})
            required_margin = float(margin_data.get("total_margin_required", 0) or 0)
            span_margin     = float(margin_data.get("span_margin",     0) or 0)
            exposure_margin = float(margin_data.get("exposure_margin", 0) or 0)
            pinfo(f"  SPAN margin     : Rs.{span_margin:,.2f}")
            pinfo(f"  Exposure margin : Rs.{exposure_margin:,.2f}")
            pinfo(f"  Required total  : Rs.{required_margin:,.2f}")
        else:
            msg = margin_resp.get("message", "") if isinstance(margin_resp, dict) else str(margin_resp)
            pwarn(f"margin() failed: {msg}")
            if MARGIN_GUARD_FAIL_OPEN:
                pwarn("Margin guard fail-open: proceeding with entry despite margin() failure")
                return True
            telegram("Margin guard: margin() API failed — trade SKIPPED")
            return False

    except Exception as exc:
        pwarn(f"margin() exception: {exc}")
        if MARGIN_GUARD_FAIL_OPEN:
            pwarn("Margin guard fail-open: proceeding with entry despite exception")
            return True
        telegram(f"Margin guard: margin() exception — trade SKIPPED\n{exc}")
        return False

    # ── Step 3: Sufficiency check ─────────────────────────────────────────────
    if required_margin <= 0:
        pwarn("Margin API returned zero — treating as unavailable, proceeding")
        return True

    required_with_buffer = required_margin * MARGIN_BUFFER
    sufficient           = total_available >= required_with_buffer
    surplus_or_shortfall = total_available - required_with_buffer

    # Store for Telegram message at entry
    state["margin_required"]  = required_margin
    state["margin_available"] = total_available

    if sufficient:
        pinfo(
            f"  MARGIN CHECK: PASS ✓  "
            f"Available Rs.{total_available:,.0f}  |  "
            f"Required Rs.{required_margin:,.0f} "
            f"(+{int((MARGIN_BUFFER - 1) * 100)}% = Rs.{required_with_buffer:,.0f})  |  "
            f"Surplus Rs.{surplus_or_shortfall:,.0f}"
        )
        psep()
        return True
    else:
        pwarn(
            f"  MARGIN CHECK: FAIL ✗  "
            f"Available Rs.{total_available:,.0f}  |  "
            f"Required Rs.{required_margin:,.0f} "
            f"(+{int((MARGIN_BUFFER - 1) * 100)}% = Rs.{required_with_buffer:,.0f})  |  "
            f"Shortfall Rs.{abs(surplus_or_shortfall):,.0f}"
        )
        psep()
        telegram(
            f"⚠️ MARGIN INSUFFICIENT — trade SKIPPED\n"
            f"Available  : Rs.{total_available:,.0f}\n"
            f"  Cash     : Rs.{available_cash:,.0f}\n"
            f"  Collateral: Rs.{collateral:,.0f}\n"
            f"Required   : Rs.{required_margin:,.0f}\n"
            f"  +{int((MARGIN_BUFFER - 1) * 100)}% buffer = Rs.{required_with_buffer:,.0f}\n"
            f"Shortfall  : Rs.{abs(surplus_or_shortfall):,.0f}\n"
            f"Action: Add funds or reduce NUMBER_OF_LOTS."
        )
        return False


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY — Short ATM Straddle (SELL CE + SELL PE)
# ═══════════════════════════════════════════════════════════════════════════════

def place_entry(expiry: str) -> bool:
    """
    Place both legs atomically via optionsmultiorder().

    Parameters
    ----------
    expiry : str
        Expiry string already resolved by job_entry() — e.g. "25MAR26".
        Passed in to avoid a second get_expiry() call (FIX-A v5.1.0).

    Success path:
      • Both legs filled → ce_active=True, pe_active=True
      • Fill prices captured via orderstatus() with retry
      • Full state saved to disk atomically
      • Returns True

    Failure paths:
      • API exception / rejected → Returns False, nothing opened
      • One leg filled, other failed → emergency close, Returns False
    """

    psep()
    pinfo("PLACING ENTRY — Short ATM Straddle  [PARTIAL SQUARE OFF]")
    pinfo(f"  Underlying  : {UNDERLYING}  |  Exchange  : {EXCHANGE}")
    pinfo(f"  Expiry      : {expiry}  |  Offset : {STRIKE_OFFSET}")
    pinfo(f"  Product     : {PRODUCT}  |  Qty/leg : {qty()}")
    pinfo(f"  CE SL will  = entry_price_CE × {1 + LEG_SL_PERCENT / 100:.2f}")
    pinfo(f"  PE SL will  = entry_price_PE × {1 + LEG_SL_PERCENT / 100:.2f}")
    pinfo(f"  Each leg managed INDEPENDENTLY — partial exit when one SL fires")
    psep()

    try:
        resp = client.optionsmultiorder(
            strategy   = STRATEGY_NAME,
            underlying = UNDERLYING,
            exchange   = EXCHANGE,
            legs       = [
                {
                    "offset"      : STRIKE_OFFSET,
                    "option_type" : "CE",
                    "action"      : "SELL",
                    "quantity"    : qty(),
                    "expiry_date" : expiry,
                    "product"     : PRODUCT,
                    "pricetype"   : "MARKET",
                    "splitsize"   : 0,
                },
                {
                    "offset"      : STRIKE_OFFSET,
                    "option_type" : "PE",
                    "action"      : "SELL",
                    "quantity"    : qty(),
                    "expiry_date" : expiry,
                    "product"     : PRODUCT,
                    "pricetype"   : "MARKET",
                    "splitsize"   : 0,
                },
            ],
        )
    except Exception as exc:
        perr(f"optionsmultiorder exception: {exc}")
        telegram(f"ENTRY EXCEPTION\n{exc}")
        return False

    if not isinstance(resp, dict) or resp.get("status") != "success":
        err = resp.get("message", str(resp)) if isinstance(resp, dict) else str(resp)
        perr(f"Entry FAILED: {err}")
        telegram(f"ENTRY FAILED\n{err}")
        return False

    # ── Parse per-leg results ─────────────────────────────────────────────────
    results     = resp.get("results", [])
    filled_legs = {}

    for leg in results:
        opt = leg.get("option_type", "")
        if leg.get("status") == "success":
            filled_legs[opt] = {
                "symbol"  : leg.get("symbol",  ""),
                "orderid" : leg.get("orderid", ""),
                "mode"    : leg.get("mode",    "live"),
            }
            pinfo(
                f"  LEG {opt} OK  | {leg.get('symbol')} "
                f"| orderid: {leg.get('orderid')} "
                f"| mode: {leg.get('mode', 'live').upper()}"
            )
        else:
            perr(f"  LEG {opt} FAILED: {leg.get('message', 'Unknown error')}")

    # ── Partial or zero entry fill → emergency close ─────────────────────────
    if "CE" not in filled_legs or "PE" not in filled_legs:
        n_filled = len(filled_legs)
        if n_filled == 0:
            perr("ENTRY FAILED — both legs rejected. No positions opened.")
            telegram("ENTRY FAILED — both legs rejected (check lot size / funds). No positions opened.")
        else:
            filled_leg  = next(iter(filled_legs))
            missing_leg = "PE" if filled_leg == "CE" else "CE"
            perr(f"PARTIAL ENTRY FILL — {filled_leg} placed but {missing_leg} failed. Emergency close triggered.")
            telegram(f"PARTIAL ENTRY FILL — {filled_leg} placed, {missing_leg} failed. Emergency close triggered. Check logs.")
            _emergency_close_all()
        return False

    # ── Populate state — BOTH legs now active ─────────────────────────────────
    now_dt = now_ist()
    state["in_position"]    = True
    state["ce_active"]      = True
    state["pe_active"]      = True
    state["symbol_ce"]      = filled_legs["CE"]["symbol"]
    state["symbol_pe"]      = filled_legs["PE"]["symbol"]
    state["orderid_ce"]     = filled_legs["CE"]["orderid"]
    state["orderid_pe"]     = filled_legs["PE"]["orderid"]
    state["underlying_ltp"] = float(resp.get("underlying_ltp", 0))
    state["entry_time"]     = now_dt.isoformat()
    state["entry_date"]     = now_dt.strftime("%Y-%m-%d")
    state["closed_pnl"]     = 0.0
    state["today_pnl"]      = 0.0
    state["exit_reason"]    = ""

    # ── Fetch average fill prices with retry (SL depends on accuracy) ────────
    _capture_fill_prices()

    # ── Persist atomically to disk immediately ────────────────────────────────
    save_state()

    trade_mode = (results[0].get("mode", "live") if results else "live").upper()
    sl_ce      = sl_level("CE")
    sl_pe      = sl_level("PE")

    pinfo("ENTRY COMPLETE")
    pinfo(f"  Mode      : {trade_mode}  NIFTY: {state['underlying_ltp']}  VIX: {state['vix_at_entry']:.2f}")
    if IVR_FILTER_ENABLED or IVP_FILTER_ENABLED:
        pinfo(
            f"  IVR       : {state['ivr_at_entry']:.1f}  |  "
            f"IVP: {state['ivp_at_entry']:.1f}%"
        )
    pinfo(f"  CE        : {state['symbol_ce']}  fill Rs.{state['entry_price_ce']:.2f}  SL @ Rs.{sl_ce:.2f}")
    pinfo(f"  PE        : {state['symbol_pe']}  fill Rs.{state['entry_price_pe']:.2f}  SL @ Rs.{sl_pe:.2f}")
    pinfo(f"  Margin used : Rs.{state['margin_required']:,.0f}  |  Available was: Rs.{state['margin_available']:,.0f}")
    pinfo(f"  State persisted → {STATE_FILE}")
    psep()

    # Build IVR/IVP line only when at least one filter is active and values captured
    ivr_ivp_line = ""
    if IVR_FILTER_ENABLED or IVP_FILTER_ENABLED:
        ivr_ivp_line = (
            f"IVR: {state['ivr_at_entry']:.1f}  |  "
            f"IVP: {state['ivp_at_entry']:.1f}%\n"
        )

    telegram(
        f"✅ ENTRY PLACED [{trade_mode}]\n"
        f"NIFTY: {state['underlying_ltp']}  VIX: {state['vix_at_entry']:.2f}\n"
        f"{ivr_ivp_line}"
        f"CE : {state['symbol_ce']}\n"
        f"  Fill Rs.{state['entry_price_ce']:.2f}  |  SL @ Rs.{sl_ce:.2f}\n"
        f"PE : {state['symbol_pe']}\n"
        f"  Fill Rs.{state['entry_price_pe']:.2f}  |  SL @ Rs.{sl_pe:.2f}\n"
        f"Expiry: {expiry}  Qty/leg: {qty()}\n"
        f"Margin used: Rs.{state['margin_required']:,.0f}"
    )
    return True


def _capture_fill_prices():
    """
    Fetch average fill prices from broker via orderstatus() for both legs.
    These are the foundation of per-leg SL levels — must be accurate.

    Retry logic: up to 5 attempts with exponential back-off (1s, 2s, 3s, 4s).
    Extra attempts vs v5.1.0 (was 3 × 1s) to handle slow broker fill reporting.
    Failure: warns and leaves entry_price at 0.0 — SL disabled for that leg.
    CRITICAL: SL cannot fire without a valid entry price.  If fill capture
    fails, the operator must monitor this trade manually until a restart.
    """
    MAX_ATTEMPTS = 5
    RETRY_DELAYS = [1.0, 2.0, 3.0, 4.0]   # delay BEFORE attempt 2, 3, 4, 5

    for leg, oid in [("CE", state["orderid_ce"]), ("PE", state["orderid_pe"])]:
        filled = False
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = client.orderstatus(order_id=oid, strategy=STRATEGY_NAME)
                if isinstance(resp, dict) and resp.get("status") == "success":
                    avg_px = float(resp.get("data", {}).get("average_price", 0) or 0)
                    if avg_px > 0:
                        state[f"entry_price_{leg.lower()}"] = avg_px
                        pinfo(f"  Fill [{leg}]: Rs.{avg_px:.2f}  (orderid: {oid}  attempt: {attempt})")
                        filled = True
                        break
                    else:
                        pdebug(f"  Fill [{leg}]: avg_px=0 on attempt {attempt}, retrying...")
                else:
                    msg = resp.get("message", "") if isinstance(resp, dict) else str(resp)
                    pdebug(f"  orderstatus [{leg}] attempt {attempt} failed: {msg}")
            except Exception as exc:
                pdebug(f"  orderstatus [{leg}] attempt {attempt} exception: {exc}")

            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAYS[attempt - 1])

        if not filled:
            pwarn(
                f"  Fill price [{leg}] unavailable after {MAX_ATTEMPTS} attempts "
                f"— SL DISABLED for this leg. MANUAL MONITORING REQUIRED."
            )
            telegram(
                f"⚠️ FILL PRICE CAPTURE FAILED — {leg} leg\n"
                f"SL is disabled for this leg.\n"
                f"Order ID: {oid}\n"
                f"MANUAL MONITORING REQUIRED until position is closed."
            )


# ═══════════════════════════════════════════════════════════════════════════════
#  SINGLE-LEG CLOSE  ← CORE OF PARTIAL SQUARE OFF
#
#  When ONE leg hits its SL, ONLY that leg is closed.
#  The other leg continues running with its own SL intact.
# ═══════════════════════════════════════════════════════════════════════════════

def close_one_leg(leg: str, reason: str, current_ltp: float = 0.0):
    """
    Close a single leg (leg = 'CE' or 'PE').

    Parameters
    ----------
    leg         : 'CE' or 'PE'
    reason      : Human-readable close reason for logs and Telegram
    current_ltp : Last known LTP for approximate P&L estimate.

    On order failure: logs + Telegram, returns WITHOUT changing state.
    The position remains open — operator must intervene manually.
    """
    leg_upper  = leg.upper()
    active_key = f"{leg.lower()}_active"
    symbol_key = f"symbol_{leg.lower()}"
    entry_key  = f"entry_price_{leg.lower()}"

    # Guard: do not double-close
    if not state[active_key]:
        pwarn(f"close_one_leg({leg_upper}) — already closed, skipping")
        return

    symbol     = state[symbol_key]
    entry_px   = state[entry_key]
    approx_pnl = (entry_px - current_ltp) * qty() if (entry_px > 0 and current_ltp > 0) else 0.0

    psep()
    pinfo(f"CLOSING {leg_upper} LEG  |  Reason: {reason}")
    pinfo(f"  Symbol      : {symbol}")
    pinfo(f"  Entry       : Rs.{entry_px:.2f}  |  LTP (approx): Rs.{current_ltp:.2f}")
    pinfo(f"  Approx P&L  : Rs.{approx_pnl:.0f}")

    # ── Place BUY MARKET to reverse the SELL position ─────────────────────────
    try:
        resp = client.placeorder(
            strategy  = STRATEGY_NAME,
            symbol    = symbol,
            exchange  = OPTION_EXCH,
            action    = "BUY",
            quantity  = qty(),
            pricetype = "MARKET",
            product   = PRODUCT,
            price     = 0,
        )
    except Exception as exc:
        perr(f"close_one_leg({leg_upper}) ORDER EXCEPTION: {exc}")
        perr(f"*** MANUAL ACTION REQUIRED — close {symbol} in broker terminal ***")
        telegram(
            f"🚨 EXIT FAILED — {leg_upper} ORDER EXCEPTION\n"
            f"MANUAL ACTION REQUIRED\n"
            f"Symbol : {symbol}\n"
            f"Error  : {exc}"
        )
        return  # State unchanged — leg still marked active

    if not (isinstance(resp, dict) and resp.get("status") == "success"):
        err = resp.get("message", str(resp)) if isinstance(resp, dict) else str(resp)
        perr(f"close_one_leg({leg_upper}) ORDER REJECTED: {err}")
        perr(f"*** MANUAL ACTION REQUIRED — close {symbol} in broker terminal ***")
        telegram(
            f"🚨 EXIT FAILED — {leg_upper} ORDER REJECTED\n"
            f"MANUAL ACTION REQUIRED\n"
            f"Symbol : {symbol}\n"
            f"Error  : {err}"
        )
        return  # State unchanged — leg still marked active

    # ── Order placed successfully — update state ──────────────────────────────
    state[active_key]   = False
    state["closed_pnl"] = state["closed_pnl"] + approx_pnl

    pinfo(f"  {leg_upper} LEG CLOSED  |  Reason: {reason}")
    pinfo(f"  Approx this-leg P&L    : Rs.{approx_pnl:.0f}")
    pinfo(f"  Cumulative closed_pnl  : Rs.{state['closed_pnl']:.0f}")

    # ── Inspect surviving leg ─────────────────────────────────────────────────
    other_leg      = "PE" if leg_upper == "CE" else "CE"
    other_active   = state[f"{other_leg.lower()}_active"]
    other_symbol   = state[f"symbol_{other_leg.lower()}"]
    other_entry_px = state[f"entry_price_{other_leg.lower()}"]
    other_sl       = sl_level(other_leg)

    if other_active:
        # ── PARTIAL EXIT — surviving leg continues ────────────────────────────
        state["in_position"] = True
        save_state()

        pinfo(f"  {other_leg} leg still ACTIVE — continues with independent SL")
        pinfo(f"  {other_leg} symbol    : {other_symbol}")
        pinfo(f"  {other_leg} entry     : Rs.{other_entry_px:.2f}")
        pinfo(f"  {other_leg} SL level  : Rs.{other_sl:.2f}  ({LEG_SL_PERCENT}%)")
        pinfo(f"  {other_leg} hard exit : {EXIT_TIME} IST")
        psep()

        telegram(
            f"⚡ PARTIAL EXIT — {leg_upper} LEG CLOSED\n"
            f"Reason     : {reason}\n"
            f"Symbol     : {symbol}\n"
            f"Approx P&L : Rs.{approx_pnl:.0f}\n"
            f"───────────────────\n"
            f"{other_leg} STILL ACTIVE\n"
            f"Symbol     : {other_symbol}\n"
            f"Entry      : Rs.{other_entry_px:.2f}\n"
            f"SL @       : Rs.{other_sl:.2f}  ({LEG_SL_PERCENT}%)\n"
            f"Hard exit  : {EXIT_TIME} IST"
        )

    else:
        # ── FULL EXIT — both legs now closed ──────────────────────────────────
        _mark_fully_flat(reason=reason)

    psep()


# ═══════════════════════════════════════════════════════════════════════════════
#  CLOSE ALL REMAINING LEGS
#  Called for: 15:15 hard exit, daily profit target, daily loss limit,
#              Ctrl+C shutdown, scheduler crash
# ═══════════════════════════════════════════════════════════════════════════════

def close_all(reason: str = "Scheduled Exit"):
    """
    Close ALL currently active legs.

    BOTH legs active → closeposition() (atomic, single API call)
    ONE leg active   → fetch LTP then close_one_leg() (with P&L estimate)
    NO legs active   → no-op

    FIX-3 (v5.0.0): When using atomic closeposition() for both legs,
    snapshot open MTM from the last monitor tick into closed_pnl before
    calling _mark_fully_flat(), so final P&L is meaningful.

    FIX-B (v5.1.0): Edge case where no monitor tick has run yet (window:
    0–15s after entry). In that case today_pnl==0 and closed_pnl==0, so
    FIX-3's snapshot gives Rs.0 even though positions have live value.
    Detection: both legs active + entry prices captured + today_pnl==0.
    Resolution: fetch live LTPs directly to compute a real open_mtm.
    Fallback: if either LTP fetch fails, use today_pnl snapshot (FIX-3).
    """
    # Acquire the RLock so this call is serialised with any running monitor tick.
    # Using RLock (not Lock) allows close_one_leg() called from within this
    # function to re-enter the lock without deadlocking.
    with _monitor_lock:
        _close_all_locked(reason)


def _close_all_locked(reason: str):
    """Inner implementation of close_all() — called while _monitor_lock is held."""
    if not state["in_position"]:
        pinfo(f"close_all() — no open position ({reason!r}), nothing to do")
        return

    active = active_legs()
    if not active:
        pinfo(f"close_all() — no active legs ({reason!r}), nothing to do")
        return

    psep()
    pinfo(f"CLOSE ALL REMAINING LEGS  |  Active: {active}  |  Reason: {reason}")

    if len(active) == 2:
        # ── Both legs open — use closeposition() for atomic close ─────────────
        pinfo("Both legs active → closeposition() (atomic)")
        try:
            resp = client.closeposition(strategy=STRATEGY_NAME)
        except Exception as exc:
            perr(f"closeposition() EXCEPTION: {exc}")
            perr("*** MANUAL ACTION REQUIRED in broker terminal ***")
            telegram(
                f"🚨 EXIT FAILED — closeposition() EXCEPTION\n"
                f"MANUAL ACTION REQUIRED\n"
                f"CE: {state['symbol_ce']}\n"
                f"PE: {state['symbol_pe']}\n"
                f"Error: {exc}"
            )
            return

        if isinstance(resp, dict) and resp.get("status") == "success":
            # ── Compute open_mtm for final P&L summary ────────────────────────
            #
            # FIX-B: If today_pnl == 0 AND both entry prices are known,
            # no monitor tick has run yet — fetch live LTPs directly.
            # This handles the 0–15s window between entry and first tick.
            #
            # Normal path (FIX-3): today_pnl was set by last monitor tick.
            #   today_pnl = closed_pnl + open_mtm
            #   → open_mtm_snapshot = today_pnl - closed_pnl
            #
            ce_px = state["entry_price_ce"]
            pe_px = state["entry_price_pe"]
            no_tick_yet = (
                state["today_pnl"] == 0.0
                and state["closed_pnl"] == 0.0
                and ce_px > 0
                and pe_px > 0
            )

            if no_tick_yet:
                pinfo("No monitor tick yet — fetching live LTPs for P&L estimate")
                ltp_ce = _fetch_ltp("CE")
                ltp_pe = _fetch_ltp("PE")
                if ltp_ce > 0 and ltp_pe > 0:
                    open_mtm_snapshot = (
                        (ce_px - ltp_ce) * qty() +
                        (pe_px - ltp_pe) * qty()
                    )
                    pinfo(
                        f"  Live LTPs: CE Rs.{ltp_ce:.2f}  PE Rs.{ltp_pe:.2f}  "
                        f"→ open_mtm Rs.{open_mtm_snapshot:.0f}"
                    )
                else:
                    pwarn("LTP fetch failed in no-tick path — P&L summary will show Rs.0")
                    open_mtm_snapshot = 0.0
            else:
                # Normal FIX-3 path: derive open_mtm from last monitor tick
                open_mtm_snapshot = state["today_pnl"] - state["closed_pnl"]

            state["closed_pnl"] += open_mtm_snapshot
            state["ce_active"]   = False
            state["pe_active"]   = False
            _mark_fully_flat(reason=reason)
        else:
            err = resp.get("message", str(resp)) if isinstance(resp, dict) else str(resp)
            perr(f"closeposition() REJECTED: {err}")
            perr("*** MANUAL ACTION REQUIRED in broker terminal ***")
            telegram(
                f"🚨 EXIT FAILED — closeposition() REJECTED\n"
                f"MANUAL ACTION REQUIRED\n"
                f"CE: {state['symbol_ce']}\n"
                f"PE: {state['symbol_pe']}\n"
                f"Error: {err}"
            )

    elif len(active) == 1:
        # ── One leg remaining — fetch LTP for accurate P&L then close ────────
        leg = active[0]
        pinfo(f"Only {leg} active → fetching LTP then close_one_leg({leg})")
        ltp = _fetch_ltp(leg)
        if ltp <= 0:
            pwarn(f"  LTP fetch failed for {leg} — P&L estimate will be Rs.0")
        close_one_leg(leg, reason=reason, current_ltp=ltp)


def _emergency_close_all():
    """
    Best-effort close for emergency scenarios (partial entry fill, orphan positions).
    Uses closeposition(). Does NOT update state — caller is responsible.
    """
    pinfo("Emergency close via closeposition()...")
    try:
        resp = client.closeposition(strategy=STRATEGY_NAME)
        if isinstance(resp, dict) and resp.get("status") == "success":
            pinfo("Emergency close: SUCCESS")
        else:
            err = resp.get("message", str(resp)) if isinstance(resp, dict) else str(resp)
            perr(f"Emergency close FAILED: {err}")
            perr("*** MANUAL ACTION REQUIRED in broker terminal ***")
    except Exception as exc:
        perr(f"Emergency close EXCEPTION: {exc}")
        perr("*** MANUAL ACTION REQUIRED in broker terminal ***")


# ═══════════════════════════════════════════════════════════════════════════════
#  MARK FULLY FLAT
#  Called when BOTH legs confirmed closed (from close_one_leg or close_all).
#  Resets position state, deletes state file, logs final summary.
# ═══════════════════════════════════════════════════════════════════════════════

def _mark_fully_flat(reason: str):
    """
    Reset all position fields, delete state file, send final summary.

    Uses state["closed_pnl"] as authoritative final P&L. It is incremented
    on each leg close and carries the correct approximate realised P&L.

    FIX-10 (v5.0.0): today_pnl was never reset to 0.0 in the reset block.
    After the position was flat, today_pnl retained the previous trade's value.
    Now explicitly reset to 0.0 in the state reset block.
    """
    final_pnl    = state["closed_pnl"]   # Authoritative — accumulated by leg closes
    duration_str = ""

    if state.get("entry_time"):
        try:
            entry_dt = datetime.fromisoformat(str(state["entry_time"]))
            if entry_dt.tzinfo is None:
                entry_dt = IST.localize(entry_dt)
            held_mins    = int((now_ist() - entry_dt).total_seconds() // 60)
            duration_str = f"  |  Held: {held_mins} min"
        except Exception:
            pass

    # Reset ALL position-related state (FIX-10: today_pnl included)
    state["in_position"]      = False
    state["ce_active"]        = False
    state["pe_active"]        = False
    state["symbol_ce"]        = ""
    state["symbol_pe"]        = ""
    state["orderid_ce"]       = ""
    state["orderid_pe"]       = ""
    state["entry_price_ce"]   = 0.0
    state["entry_price_pe"]   = 0.0
    state["closed_pnl"]       = 0.0
    state["today_pnl"]        = 0.0   # FIX-10: was missing, caused stale value after flat
    state["underlying_ltp"]   = 0.0
    state["vix_at_entry"]     = 0.0
    state["ivr_at_entry"]     = 0.0   # Reset IVR so stale value never shows in next trade
    state["ivp_at_entry"]     = 0.0   # Reset IVP so stale value never shows in next trade
    state["entry_time"]       = None
    state["entry_date"]       = None
    state["margin_required"]  = 0.0
    state["margin_available"] = 0.0
    state["exit_reason"]      = reason
    state["trade_count"]     += 1

    clear_state_file()

    sign = "+" if final_pnl >= 0 else ""
    pinfo(
        f"POSITION FULLY CLOSED  |  Reason: {reason}  |  "
        f"Final P&L ≈ Rs.{sign}{final_pnl:.0f}{duration_str}"
    )
    pinfo(f"Session trade count: {state['trade_count']}")
    psep()

    emoji = "🟢" if final_pnl >= 0 else "🔴"
    telegram(
        f"{emoji} POSITION FULLY CLOSED\n"
        f"Reason        : {reason}\n"
        f"Final P&L ≈   : Rs.{sign}{final_pnl:.0f}{duration_str}\n"
        f"Session trades: {state['trade_count']}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  INTRADAY VIX SPIKE MONITOR
#
#  _check_vix_spike() is called from _run_monitor_tick() while _monitor_lock
#  is already held.  close_all() inside uses "with _monitor_lock" — this is
#  safe because _monitor_lock is a threading.RLock (reentrant): the same thread
#  can re-acquire it without deadlocking.
#
#  Throttle: fetches VIX at most once per VIX_SPIKE_CHECK_INTERVAL_S seconds,
#  regardless of how often monitor ticks fire (every MONITOR_INTERVAL_S seconds).
#  At default settings: VIX spike check runs every 5 minutes (300s).
# ═══════════════════════════════════════════════════════════════════════════════

def _check_vix_spike():
    """
    Intraday VIX spike detector — called from _run_monitor_tick() under lock.

    Throttled to run at most once per VIX_SPIKE_CHECK_INTERVAL_S seconds so
    that a 15-second monitor loop does not hammer the VIX quotes API.

    Logic:
      1. Guard: feature enabled + position open + entry VIX captured
      2. Throttle: skip if last check was < VIX_SPIKE_CHECK_INTERVAL_S ago
      3. Fetch current live VIX
      4. Compute spike_pct = (current_vix − vix_at_entry) / vix_at_entry × 100
      5. If spike_pct >= VIX_SPIKE_THRESHOLD_PCT:
           → Telegram alert with full context
           → close_all()  (RLock re-entry is safe — same thread)

    VIX fetch failures are treated as a non-event (skipped, not a close trigger).
    We do NOT close positions on inability to fetch VIX — only on confirmed spike.
    This is the correct conservative behaviour: if VIX is unreachable, the broker
    is likely also unreachable and placing close orders would fail anyway.
    """
    global _last_vix_spike_check_time

    # ── Feature guard ─────────────────────────────────────────────────────────
    if not VIX_SPIKE_MONITOR_ENABLED:
        return

    if not state["in_position"]:
        return

    entry_vix = state.get("vix_at_entry", 0.0)
    if entry_vix <= 0:
        # Entry VIX was not captured (fetch failed at entry time). Cannot compute
        # spike %. Skip silently — this was already warned at entry.
        return

    # ── Throttle ──────────────────────────────────────────────────────────────
    now = now_ist()
    if (
        _last_vix_spike_check_time is not None
        and (now - _last_vix_spike_check_time).total_seconds() < VIX_SPIKE_CHECK_INTERVAL_S
    ):
        return   # Too soon since last check — skip this tick

    _last_vix_spike_check_time = now

    # ── Fetch live VIX ────────────────────────────────────────────────────────
    current_vix = fetch_vix()

    if current_vix <= 0:
        pwarn(
            f"VIX spike monitor: VIX unavailable — check skipped "
            f"(will retry in {VIX_SPIKE_CHECK_INTERVAL_S}s)"
        )
        return

    # Round to 2 decimal places — VIX is reported to 2dp by NSE; rounding
    # eliminates floating-point representation noise (e.g. 15.000000000001 vs 15.0)
    # and ensures the threshold comparison is clean.
    spike_pct = round((current_vix - entry_vix) / entry_vix * 100.0, 2)

    pdebug(
        f"VIX spike monitor: "
        f"entry={entry_vix:.2f}  current={current_vix:.2f}  "
        f"change={spike_pct:+.2f}%  threshold={VIX_SPIKE_THRESHOLD_PCT}%"
    )

    # ── Spike check ───────────────────────────────────────────────────────────
    if spike_pct < VIX_SPIKE_THRESHOLD_PCT:
        pinfo(
            f"VIX spike check OK: {entry_vix:.2f} → {current_vix:.2f} "
            f"({spike_pct:+.1f}% | threshold {VIX_SPIKE_THRESHOLD_PCT}%)"
        )
        return

    # ── Spike confirmed — exit immediately ────────────────────────────────────
    pwarn(
        f"VIX SPIKE DETECTED: {entry_vix:.2f} → {current_vix:.2f} "
        f"({spike_pct:+.1f}% ≥ threshold {VIX_SPIKE_THRESHOLD_PCT}%) — "
        f"closing all positions, short vega in rising IV is dangerous"
    )
    telegram(
        f"🚨 VIX SPIKE EXIT\n"
        f"Entry VIX   : {entry_vix:.2f}\n"
        f"Current VIX : {current_vix:.2f}\n"
        f"Change      : {spike_pct:+.1f}%  (threshold: +{VIX_SPIKE_THRESHOLD_PCT}%)\n"
        f"Active legs : {active_legs()}\n"
        f"Action      : Closing all positions immediately.\n"
        f"Rationale   : Short vega position in a rising-IV environment — "
        f"IV expansion erodes premium collected even on flat NIFTY."
    )
    close_all(
        reason=f"VIX Spike Exit ({entry_vix:.1f}→{current_vix:.1f}, {spike_pct:+.1f}%)"
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  P&L MONITOR  (runs every MONITOR_INTERVAL_S seconds)
#
#  PARTIAL LOGIC STEP BY STEP:
#
#  ① For each leg in [CE, PE]:
#       Skip if leg is already closed (state[leg_active] == False)
#       Fetch live LTP from broker via quotes()
#       If LTP unavailable this cycle → skip (do NOT fire SL on bad data)
#       Compute leg_mtm = (entry_price - ltp) × qty
#       Check per-leg SL: ltp >= sl_level(leg)?
#         YES → call close_one_leg(leg, ...)  ← closes ONLY this leg
#               do NOT add to open_mtm (leg is now in closed_pnl)
#               continue loop (other leg might also need SL check)
#         NO  → accumulate to open_mtm
#
#  ② combined_pnl = state["closed_pnl"] + open_mtm
#     state["today_pnl"] = combined_pnl
#
#  ③ If still in position after SL checks:
#       Check DAILY_PROFIT_TARGET → close_all() if breached
#       Check DAILY_LOSS_LIMIT    → close_all() if breached
#
#  Threading guard: _monitor_lock prevents concurrent monitor ticks
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_ltp(leg: str) -> float:
    """
    Fetch live LTP for a single leg via quotes() on NFO exchange.
    Returns float > 0 on success, or 0.0 on failure.
    """
    symbol = state[f"symbol_{leg.lower()}"]
    if not symbol:
        return 0.0
    try:
        q = client.quotes(symbol=symbol, exchange=OPTION_EXCH)
        if isinstance(q, dict) and q.get("status") == "success":
            ltp = float(q.get("data", {}).get("ltp", 0) or 0)
            return ltp if ltp > 0 else 0.0
        pwarn(f"quotes() failed [{leg}]: {q.get('message', '') if isinstance(q, dict) else str(q)}")
        return 0.0
    except Exception as exc:
        pwarn(f"quotes() exception [{leg}]: {exc}")
        return 0.0


def monitor_pnl():
    """
    Monitor tick — runs every MONITOR_INTERVAL_S seconds.
    Non-blocking lock ensures overlapping ticks are safely skipped.
    """
    if not state["in_position"]:
        return

    acquired = _monitor_lock.acquire(blocking=False)
    if not acquired:
        pwarn("Monitor tick skipped — previous tick still running (lock contention)")
        return

    try:
        _run_monitor_tick()
    finally:
        _monitor_lock.release()


def _run_monitor_tick():
    """
    Inner monitor logic — called from monitor_pnl() under lock.
    Per-leg SL check → combined P&L → daily target/limit.

    PARTIAL LOGIC:
      Each leg has its OWN independent SL. When one leg hits SL, only that
      leg is closed via close_one_leg(). The other leg keeps running.
      combined_pnl = closed_pnl (realised) + open_mtm (unrealised open legs).
    """
    open_mtm = 0.0

    for leg in ["CE", "PE"]:
        active_key = f"{leg.lower()}_active"

        if not state[active_key]:
            continue

        entry_px = state[f"entry_price_{leg.lower()}"]
        sl_lvl   = sl_level(leg)
        ltp      = _fetch_ltp(leg)

        # Skip this leg if LTP fetch failed — never fire SL on bad data
        if ltp <= 0:
            pwarn(f"LTP unavailable for {leg} this cycle — skipping SL check")
            continue

        leg_mtm = (entry_px - ltp) * qty() if entry_px > 0 else 0.0

        pdebug(
            f"  {leg} | entry Rs.{entry_px:.2f} | ltp Rs.{ltp:.2f} | "
            f"mtm Rs.{leg_mtm:.0f} | sl @ Rs.{sl_lvl:.2f}"
        )

        # ── PER-LEG SL CHECK ─────────────────────────────────────────────────
        # Fires only if: SL enabled, entry price captured, LTP breached SL level
        if LEG_SL_PERCENT > 0 and sl_lvl > 0 and ltp >= sl_lvl:
            pwarn(
                f"SL HIT: {leg}  |  LTP Rs.{ltp:.2f} >= SL Rs.{sl_lvl:.2f}  "
                f"({LEG_SL_PERCENT}% of Rs.{entry_px:.2f})"
            )
            # Close ONLY this leg — other leg keeps running
            close_one_leg(
                leg,
                reason=f"{leg} Leg SL {LEG_SL_PERCENT}% Hit",
                current_ltp=ltp,
            )
            # This leg's P&L is now in closed_pnl — not in open_mtm
            continue

        # ── No SL hit — accumulate to open MTM ───────────────────────────────
        open_mtm += leg_mtm

    # ── If both legs closed by SL(s) this tick → already flat, done ──────────
    if not state["in_position"]:
        return

    # ── Combined P&L = closed (realised) + open (unrealised) ─────────────────
    combined_pnl       = state["closed_pnl"] + open_mtm
    state["today_pnl"] = combined_pnl

    active = active_legs()
    pinfo(
        f"MONITOR | Active: {active} | "
        f"Closed P&L: Rs.{state['closed_pnl']:.0f} | "
        f"Open MTM: Rs.{open_mtm:.0f} | "
        f"Combined: Rs.{combined_pnl:.0f} | "
        f"Target: Rs.{DAILY_PROFIT_TARGET} | "
        f"Limit: Rs.{DAILY_LOSS_LIMIT}"
    )

    # ── INTRADAY VIX SPIKE CHECK (throttled — runs at most once per VIX_SPIKE_CHECK_INTERVAL_S) ──
    # Must run BEFORE daily target/limit checks so that a spike exit fires
    # even when P&L is currently positive.  After _check_vix_spike() returns
    # we must re-check in_position: the spike handler may have closed everything.
    _check_vix_spike()
    if not state["in_position"]:
        return   # VIX spike exit fired — nothing left to check

    # ── DAILY PROFIT TARGET (combined) ───────────────────────────────────────
    if DAILY_PROFIT_TARGET > 0 and combined_pnl >= DAILY_PROFIT_TARGET:
        pinfo(f"DAILY PROFIT TARGET Rs.{DAILY_PROFIT_TARGET} REACHED — closing all")
        close_all(reason=f"Daily Profit Target Rs.{DAILY_PROFIT_TARGET} Reached")
        return

    # ── DAILY LOSS LIMIT (combined) ──────────────────────────────────────────
    if DAILY_LOSS_LIMIT < 0 and combined_pnl <= DAILY_LOSS_LIMIT:
        pwarn(f"DAILY LOSS LIMIT Rs.{DAILY_LOSS_LIMIT} BREACHED — closing all")
        close_all(reason=f"Daily Loss Limit Rs.{DAILY_LOSS_LIMIT} Breached")
        return


# ═══════════════════════════════════════════════════════════════════════════════
#  STARTUP RECONCILIATION
#
#  On every restart (intentional or crash), compares saved state with live
#  positionbook() before the scheduler starts:
#
#  Case A  No state file   + broker flat       → clean start, no action
#  Case B  State: IN POS   + broker confirms   → restore full state, resume
#  Case C  State: IN POS   + broker FLAT       → externally closed, clear state
#  Case D  No state file   + broker has NFO    → orphan, emergency close
#  Stale   State from prev trading day         → MIS auto sq-off, clear state
#
#  PARTIAL-AWARE:
#  Case B restores ce_active and pe_active independently, so if the script
#  crashed mid-partial (e.g. CE closed, PE still open), the correct state
#  (ce_active=False, pe_active=True) is restored and monitoring resumes
#  for only the surviving leg.
# ═══════════════════════════════════════════════════════════════════════════════

def reconcile_on_startup():
    """Reconcile persisted state with live broker positions."""
    psep()
    pinfo("STARTUP RECONCILIATION — saved state vs live broker positions")
    psep()

    saved            = load_state()
    broker_positions = _fetch_broker_positions()

    today_str    = now_ist().date().isoformat()  # IST date — not OS date (critical fix)
    saved_date   = saved.get("entry_date", "")
    saved_in_pos = saved.get("in_position", False)

    # ── Stale state from previous trading day ─────────────────────────────────
    if saved_in_pos and saved_date and saved_date != today_str:
        pwarn(f"Stale state from {saved_date} (today: {today_str})")
        pwarn("MIS auto sq-off by broker — clearing stale state")
        state["in_position"] = False
        clear_state_file()
        telegram(
            f"RESTART: Stale state from {saved_date} cleared.\n"
            f"Starting fresh for today ({today_str})."
        )
        psep()
        return

    # ── Case A ────────────────────────────────────────────────────────────────
    if not saved_in_pos and not broker_positions:
        pinfo("Case A: No saved position + broker flat → clean start")
        psep()
        return

    # ── Case B ────────────────────────────────────────────────────────────────
    if saved_in_pos and broker_positions:
        pinfo("Case B: Saved position + broker confirms → RESTORING STATE")

        for key in state:
            if key in saved:
                state[key] = saved[key]

        # Use IST.localize() for naive datetimes — correct pytz pattern
        if isinstance(state.get("entry_time"), str):
            try:
                parsed = datetime.fromisoformat(state["entry_time"])
                if parsed.tzinfo is None:
                    state["entry_time"] = IST.localize(parsed)
                else:
                    state["entry_time"] = parsed.astimezone(IST)
            except Exception:
                state["entry_time"] = now_ist()

        active = active_legs()
        pinfo(f"  Active legs   : {active}")
        pinfo(f"  CE symbol     : {state['symbol_ce']}  active={state['ce_active']}")
        pinfo(f"  PE symbol     : {state['symbol_pe']}  active={state['pe_active']}")
        pinfo(f"  CE fill       : Rs.{state['entry_price_ce']:.2f}  SL @ Rs.{sl_level('CE'):.2f}")
        pinfo(f"  PE fill       : Rs.{state['entry_price_pe']:.2f}  SL @ Rs.{sl_level('PE'):.2f}")
        pinfo(f"  Closed P&L    : Rs.{state['closed_pnl']:.2f}")
        pinfo(f"  Entry time    : {state['entry_time']}")
        pinfo("  State restored — monitor resumes from next tick")
        psep()

        telegram(
            f"♻️ RESTARTED — STATE RESTORED\n"
            f"Active legs : {active}\n"
            f"CE: {state['symbol_ce']}\n"
            f"  Fill Rs.{state['entry_price_ce']:.2f}  SL @ Rs.{sl_level('CE'):.2f}\n"
            f"PE: {state['symbol_pe']}\n"
            f"  Fill Rs.{state['entry_price_pe']:.2f}  SL @ Rs.{sl_level('PE'):.2f}\n"
            f"Closed P&L so far: Rs.{state['closed_pnl']:.0f}\n"
            f"Monitor resuming."
        )
        return

    # ── Case C ────────────────────────────────────────────────────────────────
    if saved_in_pos and not broker_positions:
        pwarn("Case C: State=IN POSITION but broker=FLAT")
        pwarn("Position was closed externally (broker SQ-OFF / manual close)")
        state["in_position"] = False
        state["exit_reason"] = "Closed externally before restart"
        clear_state_file()
        telegram(
            f"⚠️ RESTART WARNING\n"
            f"State showed open position but broker is FLAT.\n"
            f"CE: {saved.get('symbol_ce', '?')}\n"
            f"PE: {saved.get('symbol_pe', '?')}\n"
            f"Position was closed externally. State cleared."
        )
        psep()
        return

    # ── Case D ────────────────────────────────────────────────────────────────
    if not saved_in_pos and broker_positions:
        perr("Case D: No state file but broker shows open NFO positions (ORPHAN)")
        for p in broker_positions:
            perr(f"  {p.get('symbol')} | qty: {p.get('quantity')} | avg: {p.get('average_price')}")
        perr("Attempting emergency close")
        telegram(
            f"🚨 CRITICAL: Orphan NFO positions on restart!\n"
            + "\n".join(
                f"{p.get('symbol')} qty:{p.get('quantity')}"
                for p in broker_positions
            )
            + "\nAttempting emergency close."
        )
        _emergency_close_all()
        psep()
        return

    psep()


def _fetch_broker_positions() -> list:
    """
    Fetch open NFO positions via positionbook().
    Returns list of non-zero-qty NFO positions, or [] on failure.
    """
    try:
        resp = client.positionbook()
        if not isinstance(resp, dict) or resp.get("status") != "success":
            pwarn(f"positionbook() failed: {resp}")
            return []

        all_pos  = resp.get("data", [])
        open_nfo = [
            p for p in all_pos
            if p.get("exchange", "") == OPTION_EXCH
            and int(p.get("quantity", 0) or 0) != 0
        ]

        if open_nfo:
            pinfo(f"Broker: {len(open_nfo)} open NFO position(s):")
            for p in open_nfo:
                pinfo(f"  {p.get('symbol')} | qty: {p.get('quantity')} | avg: {p.get('average_price')}")
        else:
            pinfo("Broker: no open NFO positions")

        return open_nfo

    except Exception as exc:
        pwarn(f"_fetch_broker_positions exception: {exc}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
#  SCHEDULED JOBS
# ═══════════════════════════════════════════════════════════════════════════════

def job_entry():
    """
    Entry job — fires once at ENTRY_TIME on configured DTE days.

    Filter order (short-circuit on first failure):
      1. Duplicate guard
      2. DTE filter (trading days, AlgoTest-compatible) + month filter
      3. VIX filter
      4. Margin guard (cash + collateral >= required × buffer)
      5. Reset daily counters and place straddle entry

    FIX-A (v5.1.0): expiry resolved ONCE here, passed to both
    check_margin_sufficient() and place_entry() — single source of truth,
    eliminates the double get_expiry() call and the theoretical race window
    at the Tuesday 15:30 boundary.
    """
    psep()
    pinfo(f"ENTRY JOB | {now_ist().strftime('%A %d-%b-%Y %H:%M:%S IST')}")
    psep()

    # ── 0. DTE-aware entry time guard ─────────────────────────────────────────
    #  When USE_DTE_ENTRY_MAP is True, multiple entry jobs are scheduled (one
    #  per unique time in DTE_ENTRY_TIME_MAP). Each job fires on all weekdays;
    #  this guard ensures only the job whose scheduled time matches the current
    #  DTE's configured entry time actually proceeds.
    if USE_DTE_ENTRY_MAP:
        now_hhmm = now_ist().strftime("%H:%M")
        dte_now  = get_dte()
        effective_entry = DTE_ENTRY_TIME_MAP.get(dte_now, ENTRY_TIME)
        if now_hhmm != effective_entry:
            pinfo(
                f"DTE{dte_now} effective entry is {effective_entry} — "
                f"current time {now_hhmm} does not match; this job slot skipped"
            )
            return

    # ── 1. Duplicate guard ────────────────────────────────────────────────────
    if state["in_position"]:
        pwarn("Already in position — entry skipped (duplicate guard)")
        return

    # ── 2. DTE filter + month filter ──────────────────────────────────────────
    if not dte_filter_ok():
        return

    # ── 3. VIX filter ─────────────────────────────────────────────────────────
    if not vix_ok():
        return

    # ── 4. IVR / IVP filter — uses VIX already fetched and stored by vix_ok() ─
    #  vix_ok() stores the validated VIX in state["vix_at_entry"] before returning
    #  True, so we reuse it here to avoid a second API call.
    if not ivr_ivp_ok(state["vix_at_entry"]):
        return

    # ── 5. Resolve expiry ONCE — used for both margin check and order entry ───
    expiry = get_expiry()

    # ── 6. Pre-trade margin guard ─────────────────────────────────────────────
    if not check_margin_sufficient(expiry):
        perr("Entry ABORTED — insufficient margin (cash + collateral)")
        return

    # ── 7. Reset daily counters and place entry ───────────────────────────────
    state["today_pnl"]  = 0.0
    state["closed_pnl"] = 0.0

    success = place_entry(expiry)
    if not success:
        perr("Entry FAILED — no position opened today")
        telegram("Entry FAILED — no position opened. Check logs.")


def job_exit():
    """Hard exit — fires at EXIT_TIME, closes ALL remaining active legs."""
    psep()
    pinfo(f"EXIT JOB | {now_ist().strftime('%A %d-%b-%Y %H:%M:%S IST')}")
    psep()

    if not state["in_position"]:
        pinfo("No open position at scheduled exit — nothing to do")
        return

    active = active_legs()
    pinfo(f"Hard exit — active legs: {active}")
    close_all(reason=f"Scheduled Hard Exit at {EXIT_TIME}")


def job_monitor():
    """Monitor tick — fires every MONITOR_INTERVAL_S seconds."""
    if state["in_position"]:
        monitor_pnl()


def job_update_vix_history():
    """
    Daily VIX history maintenance job — fires once at VIX_UPDATE_TIME (15:30 IST).

    Purpose: append today's closing VIX value to VIX_HISTORY_FILE so that the
    IVR/IVP filter has fresh data for tomorrow's entry decision.

    Logic:
      1. Skip on weekends — VIX_UPDATE_TIME job fires mon-fri in the scheduler.
         The weekend guard here protects against manual calls on Saturdays/Sundays.
      2. Check for duplicate — if today's date is already the last row, skip
         (handles script restart after 15:30 gracefully, no double-entry).
      3. Fetch live VIX via fetch_vix() — the same API used by the entry filter.
         At 15:30 this is effectively the closing price.
      4. Append new row to the file using atomic write (temp-file + rename),
         same pattern as save_state() — crash-safe, no partial writes.
      5. Trim file to last 300 rows to prevent unbounded growth while keeping
         a comfortable buffer beyond the 252-row window needed for IVR/IVP.

    On VIX fetch failure: logs warning + Telegram.  Does NOT raise — a missed
    daily update is not fatal; the previous day's data remains valid for tomorrow.
    """
    now_dt = now_ist()

    # Weekend guard — belt-and-suspenders (scheduler fires mon-fri anyway)
    if now_dt.weekday() >= 5:
        pdebug("VIX history update: weekend — skipping")
        return

    today_str = now_dt.date().isoformat()

    # ── Duplicate guard — avoid double-appending on restart after 15:30 ──────
    rows = _load_vix_history_raw()
    if rows and rows[-1][0] == today_str:
        pdebug(
            f"VIX history: {today_str} already recorded "
            f"(VIX {rows[-1][1]:.2f}) — no update needed"
        )
        return

    # ── Fetch today's closing VIX ─────────────────────────────────────────────
    vix = fetch_vix()
    if vix <= 0:
        pwarn(f"VIX history update: VIX unavailable for {today_str} — skipping")
        telegram(
            f"⚠️ VIX history: daily update FAILED for {today_str}\n"
            f"IVR/IVP data will be 1 day stale tomorrow.\n"
            f"Check OpenAlgo / NSE connectivity."
        )
        return

    # ── Append new row and trim to 300 rows ───────────────────────────────────
    rows.append((today_str, vix))
    if len(rows) > 300:
        rows = rows[-300:]

    # Atomic write — same temp-rename pattern as save_state()
    try:
        hist_dir = os.path.dirname(os.path.abspath(VIX_HISTORY_FILE)) or "."
        fd, tmp_path = tempfile.mkstemp(dir=hist_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write("date,vix_close\n")
                for d, v in rows:
                    f.write(f"{d},{v:.2f}\n")
            os.replace(tmp_path, VIX_HISTORY_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        pinfo(
            f"VIX history updated: {today_str} → VIX {vix:.2f}  "
            f"({len(rows)} rows in {VIX_HISTORY_FILE})"
        )
    except Exception as exc:
        pwarn(f"VIX history write failed: {exc}")
        telegram(f"⚠️ VIX history write FAILED: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
#  STARTUP BANNER
#  FIX-2 (v5.0.0): compute expiry date once, reuse for all DTE label lookups
#  instead of calling get_expiry() (which logs) once per DTE value.
# ═══════════════════════════════════════════════════════════════════════════════

def _print_banner():
    dte_str  = ", ".join(f"DTE{d}" for d in sorted(TRADE_DTE))
    skip_str = ", ".join(MONTH_NAMES[m] for m in sorted(SKIP_MONTHS)) if SKIP_MONTHS else "None"
    guard_str = (
        f"ENABLED  buffer={int((MARGIN_BUFFER - 1) * 100)}%  "
        f"fail_open={MARGIN_GUARD_FAIL_OPEN}"
        if MARGIN_GUARD_ENABLED else "DISABLED"
    )

    # FIX-2: compute expiry date ONCE, silently, before looping over TRADE_DTE
    def _dte_to_dayname(dte_val: int, expiry_date: date) -> str:
        """
        Return the weekday name for a given trading-day DTE offset.
        Walks backward from expiry_date by dte_val trading days.
        """
        try:
            d     = expiry_date
            count = 0
            while count < dte_val:
                d -= timedelta(days=1)
                if d.weekday() < 5:
                    count += 1
            return DAY_NAMES[d.weekday()]
        except Exception:
            return "?"

    expiry_date = _get_expiry_date_silent()
    day_str = " | ".join(
        f"DTE{d}={_dte_to_dayname(d, expiry_date)}" for d in sorted(TRADE_DTE)
    )

    print("", flush=True)
    print("=" * 72, flush=True)
    print(f"  NIFTY TRENDING STRADDLE  v{VERSION}  —  PARTIAL SQUARE OFF", flush=True)
    print(f"  OpenAlgo + Dhan API  |  Restart-Safe  |  Production Grade", flush=True)
    print("=" * 72, flush=True)
    print(f"  Host             : {OPENALGO_HOST}", flush=True)
    print(f"  Strategy         : {STRATEGY_NAME}", flush=True)
    print(f"  Underlying       : {UNDERLYING}  |  Exchange  : {EXCHANGE}", flush=True)
    print(f"  Lot size         : {LOT_SIZE}  |  Lots : {NUMBER_OF_LOTS}  |  Qty/leg : {qty()}", flush=True)
    print(f"  Strike offset    : {STRIKE_OFFSET}  |  Product : {PRODUCT}", flush=True)
    if USE_DTE_ENTRY_MAP:
        dte_entry_str = "  |  ".join(
            f"DTE{d}={DTE_ENTRY_TIME_MAP[d]}" for d in sorted(DTE_ENTRY_TIME_MAP)
        )
        print(f"  Entry (DTE-map)  : {dte_entry_str}  |  Hard exit : {EXIT_TIME} IST", flush=True)
    else:
        print(f"  Entry            : {ENTRY_TIME} IST  |  Hard exit : {EXIT_TIME} IST", flush=True)
    print(f"  Monitor interval : every {MONITOR_INTERVAL_S}s", flush=True)
    print(f"  DTE filter       : {dte_str}  ({day_str})", flush=True)
    print(f"  Skip months      : {skip_str}", flush=True)
    print(f"  VIX filter       : {VIX_MIN}–{VIX_MAX}  (enabled: {VIX_FILTER_ENABLED})", flush=True)

    # IVR / IVP filter display
    ivr_str = f"IVR>={IVR_MIN}" if IVR_FILTER_ENABLED else "IVR=disabled"
    ivp_str = f"IVP>={IVP_MIN}%" if IVP_FILTER_ENABLED else "IVP=disabled"
    print(
        f"  IVR/IVP filter   : {ivr_str}  |  {ivp_str}  |  "
        f"fail_open={IVR_FAIL_OPEN}  |  history={VIX_HISTORY_FILE}",
        flush=True,
    )

    # VIX spike monitor display
    spike_str = (
        f"ENABLED  threshold={VIX_SPIKE_THRESHOLD_PCT}%  "
        f"check_every={VIX_SPIKE_CHECK_INTERVAL_S}s"
        if VIX_SPIKE_MONITOR_ENABLED else "DISABLED"
    )
    print(f"  VIX spike monitor: {spike_str}", flush=True)

    print(f"  Sq-off mode      : PARTIAL — each leg has independent {LEG_SL_PERCENT}% SL", flush=True)
    print(f"  Daily target     : Rs.{DAILY_PROFIT_TARGET_PER_LOT}/lot × {NUMBER_OF_LOTS} lot(s) = Rs.{DAILY_PROFIT_TARGET}  (0=disabled)", flush=True)
    print(f"  Daily limit      : Rs.{DAILY_LOSS_LIMIT_PER_LOT}/lot × {NUMBER_OF_LOTS} lot(s) = Rs.{DAILY_LOSS_LIMIT}   (0=disabled)", flush=True)
    print(f"  Margin guard     : {guard_str}", flush=True)
    print(f"  Auto expiry      : {AUTO_EXPIRY}  |  Manual : {MANUAL_EXPIRY}  (NIFTY expires TUESDAY)", flush=True)
    print(f"  State file       : {os.path.abspath(STATE_FILE)}", flush=True)
    print(f"  Telegram         : {TELEGRAM_ENABLED}", flush=True)
    print("=" * 72, flush=True)
    print(f"  Backtest (2019-2026): P&L Rs.5,04,192 | Win 66.71% | MaxDD Rs.34,179", flush=True)
    print(f"  Avg/trade Rs.289 | Return/MDD 1.38 | 1746 trades", flush=True)
    print("─" * 72, flush=True)
    print(f"  Partial logic : each leg has its OWN {LEG_SL_PERCENT}% SL.", flush=True)
    print(f"  SL on CE → close CE only. PE continues with its own SL.", flush=True)
    print(f"  SL on PE → close PE only. CE continues with its own SL.", flush=True)
    print(f"  combined P&L  = closed_pnl + open_leg_mtm", flush=True)
    print(f"  DTE method    : TRADING days (AlgoTest-compatible, weekends excluded).", flush=True)
    print(f"  DTE filter    : trades ONLY on {dte_str} of weekly expiry cycle.", flush=True)
    print(f"  Margin guard  : funds() + margin() checked before every entry.", flush=True)
    print(f"  Analyze Mode  : paper/live is set in the OpenAlgo dashboard.", flush=True)
    print("=" * 72, flush=True)
    print("", flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  MANUAL CONTROLS
# ═══════════════════════════════════════════════════════════════════════════════

def check_connection():
    """Test OpenAlgo connection and display account funds + collateral."""
    psep()
    pinfo("Testing OpenAlgo connection...")
    try:
        resp = client.funds()
        if isinstance(resp, dict) and resp.get("status") == "success":
            data = resp.get("data", {})
            cash = float(data.get("availablecash",  0) or 0)
            coll = float(data.get("collateral",     0) or 0)
            used = float(data.get("utiliseddebits", 0) or 0)
            m2m  = float(data.get("m2munrealized",  0) or 0)
            pinfo(f"Connection       : OK")
            pinfo(f"Available cash   : Rs.{cash:,.2f}")
            pinfo(f"Collateral       : Rs.{coll:,.2f}")
            pinfo(f"Total available  : Rs.{cash + coll:,.2f}")
            pinfo(f"Utilised debits  : Rs.{used:,.2f}")
            pinfo(f"M2M Unrealised   : Rs.{m2m:,.2f}")
        else:
            perr(f"Connection FAILED: {resp}")
    except Exception as exc:
        perr(f"Connection exception: {exc}")
    psep()


def manual_entry():
    """Force entry immediately — bypasses time check, runs all other filters."""
    pinfo("MANUAL ENTRY triggered")
    job_entry()


def manual_exit():
    """Force close all active legs immediately."""
    pinfo("MANUAL EXIT triggered")
    close_all(reason="Manual Exit by Operator")


def show_state():
    """Print full in-memory state + computed SL levels + DTE info to stdout."""
    psep()
    pinfo("STATE DUMP:")
    for k, v in state.items():
        print(f"    {k:<24} : {v}", flush=True)
    print(f"    {'sl_ce (computed)':<24} : Rs.{sl_level('CE'):.2f}", flush=True)
    print(f"    {'sl_pe (computed)':<24} : Rs.{sl_level('PE'):.2f}", flush=True)
    print(f"    {'active_legs':<24} : {active_legs()}", flush=True)

    dte        = get_dte()
    weekday    = now_ist().date().weekday()   # IST weekday — not OS date
    is_weekend = weekday >= 5

    print(f"    {'current_dte':<24} : DTE{dte} ({DAY_NAMES[weekday]})", flush=True)

    if is_weekend:
        print(
            f"    {'dte_in_filter':<24} : False  "
            f"(weekend — blocked by weekend guard regardless of DTE)",
            flush=True
        )
    else:
        print(
            f"    {'dte_in_filter':<24} : {dte in TRADE_DTE}  "
            f"(TRADE_DTE={['DTE' + str(d) for d in sorted(TRADE_DTE)]})",
            flush=True
        )
    psep()


def check_margin_now():
    """Manual margin check — test without placing a trade."""
    expiry = get_expiry()
    result = check_margin_sufficient(expiry)
    pinfo(f"Margin check result: {'PASS ✓' if result else 'FAIL ✗'}")


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def _validate_config():
    """
    Sanity-check all configuration constants at startup.
    Raises ValueError on any fatal misconfiguration — prevents a live trade
    being placed with wrong parameters due to a typo in the config section.
    """
    errors = []

    # ── API credentials ───────────────────────────────────────────────────────
    if OPENALGO_API_KEY in ("", "your_openalgo_api_key_here"):
        errors.append("OPENALGO_API_KEY is not set (still placeholder)")

    # ── Lot / quantity ────────────────────────────────────────────────────────
    if LOT_SIZE <= 0:
        errors.append(f"LOT_SIZE must be > 0, got {LOT_SIZE}")
    if NUMBER_OF_LOTS <= 0:
        errors.append(f"NUMBER_OF_LOTS must be > 0, got {NUMBER_OF_LOTS}")

    # ── Risk parameters ───────────────────────────────────────────────────────
    if LEG_SL_PERCENT < 0:
        errors.append(f"LEG_SL_PERCENT must be >= 0, got {LEG_SL_PERCENT}")
    if DAILY_PROFIT_TARGET_PER_LOT < 0:
        errors.append(f"DAILY_PROFIT_TARGET_PER_LOT must be >= 0 (use 0 to disable), got {DAILY_PROFIT_TARGET_PER_LOT}")
    if DAILY_LOSS_LIMIT_PER_LOT > 0:
        errors.append(f"DAILY_LOSS_LIMIT_PER_LOT must be <= 0 (negative = loss, use 0 to disable), got {DAILY_LOSS_LIMIT_PER_LOT}")

    # ── Margin guard ──────────────────────────────────────────────────────────
    if MARGIN_BUFFER < 1.0:
        errors.append(f"MARGIN_BUFFER must be >= 1.0 (e.g. 1.20 = 20% headroom), got {MARGIN_BUFFER}")
    if ATM_STRIKE_ROUNDING <= 0:
        errors.append(f"ATM_STRIKE_ROUNDING must be > 0, got {ATM_STRIKE_ROUNDING}")

    # ── Timing ────────────────────────────────────────────────────────────────
    try:
        eh, em = parse_hhmm(ENTRY_TIME)
        assert 0 <= eh <= 23 and 0 <= em <= 59
    except Exception:
        errors.append(f"ENTRY_TIME invalid: {ENTRY_TIME!r}  (expected HH:MM)")
    if USE_DTE_ENTRY_MAP:
        for dte_key, t in DTE_ENTRY_TIME_MAP.items():
            try:
                dh, dm = parse_hhmm(t)
                assert 0 <= dh <= 23 and 0 <= dm <= 59
            except Exception:
                errors.append(f"DTE_ENTRY_TIME_MAP[{dte_key}] invalid: {t!r}  (expected HH:MM)")
    try:
        xh, xm = parse_hhmm(EXIT_TIME)
        assert 0 <= xh <= 23 and 0 <= xm <= 59
    except Exception:
        errors.append(f"EXIT_TIME invalid: {EXIT_TIME!r}  (expected HH:MM)")
    if MONITOR_INTERVAL_S <= 0:
        errors.append(f"MONITOR_INTERVAL_S must be > 0, got {MONITOR_INTERVAL_S}")

    # ── DTE filter ────────────────────────────────────────────────────────────
    if not TRADE_DTE:
        errors.append("TRADE_DTE is empty — no trading days configured")
    if any(d < 0 for d in TRADE_DTE):
        errors.append(f"TRADE_DTE contains negative values: {TRADE_DTE}")

    # ── VIX filter ────────────────────────────────────────────────────────────
    if VIX_FILTER_ENABLED and VIX_MIN >= VIX_MAX:
        errors.append(f"VIX_MIN ({VIX_MIN}) must be < VIX_MAX ({VIX_MAX})")

    # ── Expiry ────────────────────────────────────────────────────────────────
    if not AUTO_EXPIRY:
        try:
            manual_dt = datetime.strptime(MANUAL_EXPIRY, "%d%b%y")
            if manual_dt.weekday() != 1:   # 1 = Tuesday
                errors.append(
                    f"MANUAL_EXPIRY {MANUAL_EXPIRY!r} is a {DAY_NAMES[manual_dt.weekday()]} "
                    f"— NIFTY weekly expiry is on Tuesday"
                )
        except Exception:
            errors.append(f"MANUAL_EXPIRY format invalid: {MANUAL_EXPIRY!r}  (expected DDMMMYY e.g. 25MAR26)")

    # ── IVR / IVP filter ──────────────────────────────────────────────────────
    if IVR_FILTER_ENABLED and not (0.0 <= IVR_MIN <= 100.0):
        errors.append(f"IVR_MIN must be 0–100, got {IVR_MIN}")
    if IVP_FILTER_ENABLED and not (0.0 <= IVP_MIN <= 100.0):
        errors.append(f"IVP_MIN must be 0–100, got {IVP_MIN}")
    if (IVR_FILTER_ENABLED or IVP_FILTER_ENABLED) and not VIX_HISTORY_FILE:
        errors.append("VIX_HISTORY_FILE must not be empty when IVR/IVP filter is enabled")
    if VIX_HISTORY_MIN_ROWS <= 0:
        errors.append(f"VIX_HISTORY_MIN_ROWS must be > 0, got {VIX_HISTORY_MIN_ROWS}")
    try:
        vuh, vum = parse_hhmm(VIX_UPDATE_TIME)
        assert 0 <= vuh <= 23 and 0 <= vum <= 59
    except Exception:
        errors.append(f"VIX_UPDATE_TIME invalid: {VIX_UPDATE_TIME!r}  (expected HH:MM)")

    # ── VIX spike monitor ─────────────────────────────────────────────────────
    if VIX_SPIKE_MONITOR_ENABLED and VIX_SPIKE_THRESHOLD_PCT <= 0:
        errors.append(
            f"VIX_SPIKE_THRESHOLD_PCT must be > 0 when VIX_SPIKE_MONITOR_ENABLED=True, "
            f"got {VIX_SPIKE_THRESHOLD_PCT}"
        )
    if VIX_SPIKE_CHECK_INTERVAL_S <= 0:
        errors.append(f"VIX_SPIKE_CHECK_INTERVAL_S must be > 0, got {VIX_SPIKE_CHECK_INTERVAL_S}")
    if VIX_SPIKE_MONITOR_ENABLED and VIX_SPIKE_CHECK_INTERVAL_S < MONITOR_INTERVAL_S:
        errors.append(
            f"VIX_SPIKE_CHECK_INTERVAL_S ({VIX_SPIKE_CHECK_INTERVAL_S}s) must be >= "
            f"MONITOR_INTERVAL_S ({MONITOR_INTERVAL_S}s) — "
            f"spike check cannot run faster than the monitor loop"
        )

    # ── Telegram ──────────────────────────────────────────────────────────────
    if TELEGRAM_ENABLED and (not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID):
        # Warn, don't block — Telegram is advisory only
        pwarn("CONFIG: TELEGRAM_ENABLED=True but BOT_TOKEN or CHAT_ID is empty — alerts will be silent")

    # ── Report ────────────────────────────────────────────────────────────────
    if errors:
        perr("═" * 68)
        perr("CONFIGURATION ERRORS — strategy will NOT start:")
        for e in errors:
            perr(f"  ✗  {e}")
        perr("═" * 68)
        raise ValueError(f"Config validation failed with {len(errors)} error(s). See logs above.")

    pinfo("Config validation: all checks passed ✓")


def run():
    """
    Production startup:
      1. Validate configuration
      2. Print banner
      3. Test connection + show funds
      4. Reconcile state with broker
      5. Start APScheduler (entry / exit / monitor)
      6. Graceful shutdown on Ctrl+C, SIGTERM, or crash
    """
    _validate_config()   # Raises on misconfiguration — must be first
    _print_banner()
    check_connection()
    reconcile_on_startup()
    _check_vix_history_on_startup()   # Log VIX history file status (advisory only)

    exit_h,  exit_m  = parse_hhmm(EXIT_TIME)
    vix_upd_h, vix_upd_m = parse_hhmm(VIX_UPDATE_TIME)

    scheduler = BlockingScheduler(timezone=IST)

    # ── Entry job(s) ──────────────────────────────────────────────────────────
    if USE_DTE_ENTRY_MAP:
        # Schedule one job per unique entry time in the map.
        # Each job fires on all weekdays (mon-fri); the DTE-aware guard inside
        # job_entry() ensures only the matching time slot actually enters.
        unique_times = sorted(set(DTE_ENTRY_TIME_MAP.values()))
        for t in unique_times:
            th, tm = parse_hhmm(t)
            scheduler.add_job(
                func               = job_entry,
                trigger            = "cron",
                day_of_week        = "mon-fri",
                hour               = th,
                minute             = tm,
                id                 = f"entry_job_{t.replace(':', '')}",
                name               = f"Entry {t} (DTE-map)",
                misfire_grace_time = 60,
            )
        pinfo(f"DTE-aware entry scheduled at: {', '.join(unique_times)} IST")
    else:
        entry_h, entry_m = parse_hhmm(ENTRY_TIME)
        scheduler.add_job(
            func               = job_entry,
            trigger            = "cron",
            day_of_week        = "mon-fri",
            hour               = entry_h,
            minute             = entry_m,
            id                 = "entry_job",
            name               = f"Entry {ENTRY_TIME}",
            misfire_grace_time = 60,
        )
    scheduler.add_job(
        func               = job_exit,
        trigger            = "cron",
        day_of_week        = "mon-fri",
        hour               = exit_h,
        minute             = exit_m,
        id                 = "exit_job",
        name               = f"Exit {EXIT_TIME}",
        misfire_grace_time = 120,
    )
    scheduler.add_job(
        func    = job_monitor,
        trigger = "interval",
        seconds = MONITOR_INTERVAL_S,
        id      = "monitor_job",
        name    = f"Monitor {MONITOR_INTERVAL_S}s",
    )
    # VIX history daily update — runs once at VIX_UPDATE_TIME after market close.
    # Appends today's closing VIX to vix_history.csv so tomorrow's IVR/IVP
    # filter has fresh data.  Scheduled mon-fri; job itself has a weekend guard.
    scheduler.add_job(
        func               = job_update_vix_history,
        trigger            = "cron",
        day_of_week        = "mon-fri",
        hour               = vix_upd_h,
        minute             = vix_upd_m,
        id                 = "vix_history_job",
        name               = f"VIX History Update {VIX_UPDATE_TIME}",
        misfire_grace_time = 300,   # 5-min grace — if delayed by scheduler restart
    )
    pinfo(f"VIX history update scheduled at {VIX_UPDATE_TIME} IST (mon-fri)")

    dte_str = ", ".join(f"DTE{d}" for d in sorted(TRADE_DTE))
    if USE_DTE_ENTRY_MAP:
        entry_display = "DTE-map " + ", ".join(
            f"DTE{d}={DTE_ENTRY_TIME_MAP[d]}" for d in sorted(DTE_ENTRY_TIME_MAP)
        )
    else:
        entry_display = ENTRY_TIME
    pinfo(
        f"Scheduler running | Entry: {entry_display} | "
        f"Exit: {EXIT_TIME} | Monitor: every {MONITOR_INTERVAL_S}s"
    )
    pinfo("Press Ctrl+C to stop gracefully  |  systemd sends SIGTERM — both handled")
    print("", flush=True)

    # ── SIGTERM handler (systemd / docker stop / kill) ────────────────────────
    # APScheduler's BlockingScheduler converts SIGTERM → SystemExit by default,
    # which is caught below. This explicit handler ensures we log and close open
    # positions even on SIGTERM before the scheduler handles it.
    def _sigterm_handler(signum, frame):                          # noqa: ANN001
        pinfo("SIGTERM received — initiating graceful shutdown")
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, _sigterm_handler)
    except (OSError, ValueError):
        pass   # Not supported on this platform (e.g. Windows threads)

    guard_status = (
        f"Margin guard: ENABLED ({int((MARGIN_BUFFER - 1) * 100)}% buffer, "
        f"fail_open={MARGIN_GUARD_FAIL_OPEN})"
        if MARGIN_GUARD_ENABLED else "Margin guard: DISABLED"
    )
    if USE_DTE_ENTRY_MAP:
        tg_entry = "DTE-map: " + ", ".join(
            f"DTE{d}={DTE_ENTRY_TIME_MAP[d]}" for d in sorted(DTE_ENTRY_TIME_MAP)
        )
    else:
        tg_entry = ENTRY_TIME
    ivr_tg  = f"IVR>={IVR_MIN}" if IVR_FILTER_ENABLED else "off"
    ivp_tg  = f"IVP>={IVP_MIN}%" if IVP_FILTER_ENABLED else "off"
    spike_tg = (
        f"VIX spike: +{VIX_SPIKE_THRESHOLD_PCT}% → exit (check/{VIX_SPIKE_CHECK_INTERVAL_S}s)"
        if VIX_SPIKE_MONITOR_ENABLED else "VIX spike monitor: off"
    )
    telegram(
        f"🚀 Strategy STARTED v{VERSION} [PARTIAL]\n"
        f"Entry: {tg_entry}  Hard Exit: {EXIT_TIME}\n"
        f"Qty/leg: {NUMBER_OF_LOTS}×{LOT_SIZE} = {qty()}\n"
        f"Leg SL: {LEG_SL_PERCENT}% (independent per leg)\n"
        f"VIX: {VIX_MIN}–{VIX_MAX}  |  {ivr_tg}  |  {ivp_tg}\n"
        f"{spike_tg}\n"
        f"DTE filter: {dte_str}  (trading days, AlgoTest-compatible)\n"
        f"Skip months: {', '.join(MONTH_NAMES[m] for m in sorted(SKIP_MONTHS)) if SKIP_MONTHS else 'None'}\n"
        f"Target: Rs.{DAILY_PROFIT_TARGET_PER_LOT}/lot = Rs.{DAILY_PROFIT_TARGET}  "
        f"Limit: Rs.{DAILY_LOSS_LIMIT_PER_LOT}/lot = Rs.{DAILY_LOSS_LIMIT}\n"
        f"{guard_status}"
    )

    try:
        scheduler.start()

    except (KeyboardInterrupt, SystemExit):
        pinfo("Strategy stopped by operator (Ctrl+C / SIGTERM)")
        if state["in_position"]:
            pwarn(f"Open legs on shutdown: {active_legs()} — closing for safety")
            close_all(reason="Emergency: Script Stopped by Operator")
        telegram("Strategy STOPPED by operator")

    except Exception as exc:
        perr(f"Scheduler crashed: {exc}")
        if state["in_position"]:
            perr(f"Open legs: {active_legs()} — attempting emergency close")
            close_all(reason="Emergency: Scheduler Crash")
        telegram(f"🚨 Strategy CRASHED\n{exc}\nCheck logs immediately.")
        raise


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Default: run() — full production scheduler.

    For testing (comment run(), uncomment one):
      check_connection()   → verify OpenAlgo + show funds + collateral
      check_margin_now()   → test margin guard without placing a trade
      manual_entry()       → force entry now (bypasses time, runs all filters)
      manual_exit()        → close all active legs now
      show_state()         → dump full state + SL levels + DTE info
    """

    # ── Production ────────────────────────────────────────────────────────────
    run()

    # ── Testing (uncomment one at a time) ─────────────────────────────────────
    # check_connection()
    # check_margin_now()
    # manual_entry()
    # manual_exit()
    # show_state()