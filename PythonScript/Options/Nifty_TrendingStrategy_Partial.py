"""
╔══════════════════════════════════════════════════════════════════════════════════╗
║   NIFTY TRENDING STRATEGY  —  PARTIAL SQUARE OFF   v4.0.0                     ║
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
║   v4.0.0 NEW FEATURES                                                           ║
║   • PRE-TRADE MARGIN GUARD: checks funds() + margin() basket before entry      ║
║     - Available cash + collateral must cover required margin × safety buffer    ║
║     - Basket call gets straddle SPAN offset (cheaper than two naked shorts)    ║
║     - Fail-open design: API failure allows trade (prevents false skips)         ║
║   • ATM STRIKE FETCH: LTP-based ATM strike for accurate margin estimate        ║
║   • ENHANCED BANNER: shows margin guard config at startup                      ║
║   • ATOMIC STATE WRITE: temp-file + rename prevents corrupt state on crash     ║
║   • FILL PRICE RETRY: 3 attempts with 1s delay to fetch orderstatus fills      ║
║   • MONITOR THREAD GUARD: concurrent monitor calls blocked via threading.Lock  ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║   RESTART SAFETY (unchanged + improved)                                         ║
║   • State persisted atomically after every single mutation                     ║
║   • Each leg's active/closed status tracked and persisted independently        ║
║   • On restart: reconciles with live positionbook() before scheduler starts    ║
║   • 5 cases handled: fresh / restore partial / restore full /                  ║
║                       externally closed / stale / orphan                       ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║   QUICK START                                                                    ║
║   1.  pip install openalgo apscheduler pytz requests                            ║
║   2.  Set environment variables (or fill directly in SECTION 1 and 10):        ║
║       export OPENALGO_APIKEY="your_key"                                         ║
║       export TELEGRAM_BOT_TOKEN="your_token"                                    ║
║       export TELEGRAM_CHAT_ID="your_chat_id"                                    ║
║   3.  Sync Master Contract in OpenAlgo dashboard before 09:00                  ║
║   4.  Enable Analyze Mode in OpenAlgo dashboard (paper trade first)            ║
║   5.  python Nifty_TrendingStrategy_v4.py                                      ║
║   6.  After satisfied with paper trades → disable Analyze Mode → goes LIVE     ║
╠══════════════════════════════════════════════════════════════════════════════════╣
║   SYMBOL FORMAT  (docs.openalgo.in)                                             ║
║   Order entry   : NIFTY  on  NSE_INDEX  (OpenAlgo resolves ATM strike)         ║
║   Option quotes : NIFTY17MAR2623000CE  on  NFO  (Tuesday expiry)          ║
║   VIX           : INDIAVIX  on  NSE_INDEX                                      ║
╚══════════════════════════════════════════════════════════════════════════════════╝
"""

import os
import json
import time
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
LOT_SIZE       = 75            # NIFTY=75  BANKNIFTY=35  FINNIFTY=40
NUMBER_OF_LOTS = 1             # Lots per leg — start with 1 for paper trading
PRODUCT        = "MIS"         # MIS = intraday auto sq-off  |  NRML = carry forward
STRIKE_OFFSET  = "ATM"         # ATM | OTM1..OTM5 | ITM1..ITM5

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 3 — TIMING  (IST 24h HH:MM)
# ═══════════════════════════════════════════════════════════════════════════════

ENTRY_TIME         = "09:17"   # Straddle entry time
EXIT_TIME          = "15:15"   # Hard square-off — closes ALL remaining open legs
MONITOR_INTERVAL_S = 15        # Seconds between P&L / SL checks

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 4 — TRADE DAY FILTER
#  Backtest day-wise P&L (Wed+Thu+Fri = 81% of total P&L):
#    Thu Rs.2,12,547  |  Fri Rs.1,08,537  |  Wed Rs.87,880
#  0=Mon 1=Tue 2=Wed 3=Thu 4=Fri
# ═══════════════════════════════════════════════════════════════════════════════

TRADE_DAYS = [2, 3, 4]            # Wed + Thu + Fri  (highest alpha days)
# TRADE_DAYS = [3]                # Thursday only
# TRADE_DAYS = [0, 1, 2, 3, 4]   # All weekdays

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
#  SECTION 7 — RISK MANAGEMENT
#
#  LEG_SL_PERCENT — applied to EACH LEG INDEPENDENTLY
#    CE SL = CE_entry_price × (1 + LEG_SL_PERCENT/100)  → close CE only
#    PE SL = PE_entry_price × (1 + LEG_SL_PERCENT/100)  → close PE only
#    99.6% accuracy verified from AlgoTest CSV (2311 SL hits, median = 20.00%)
#
#  DAILY_PROFIT_TARGET / DAILY_LOSS_LIMIT — evaluated on COMBINED P&L
#    combined = closed_leg_pnl + open_leg(s)_mtm
#    Trips → close ALL remaining open legs immediately
# ═══════════════════════════════════════════════════════════════════════════════

LEG_SL_PERCENT      = 20.0     # % of entry premium per leg  (0 = disabled)
DAILY_PROFIT_TARGET =  5000    # Combined Rs. target  (0 = disabled)
DAILY_LOSS_LIMIT    = -4000    # Combined Rs. loss limit — NEGATIVE  (0 = disabled)

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 7A — PRE-TRADE MARGIN GUARD  (NEW in v4.0.0)
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
#  SECTION 8 — EXPIRY
#  Format : DDMMMYY uppercase  e.g. "19MAR26"
# ═══════════════════════════════════════════════════════════════════════════════

AUTO_EXPIRY   = True           # True = auto nearest Tuesday (NIFTY weekly expiry day)
MANUAL_EXPIRY = "17MAR26"      # Used only when AUTO_EXPIRY = False  (must be a Tuesday)

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

VERSION     = "4.0.0"
IST         = pytz.timezone("Asia/Kolkata")
OPTION_EXCH = "NFO"        # All F&O option contracts (quotes / positions)
INDEX_EXCH  = "NSE_INDEX"  # Underlying index + VIX (order entry)
VIX_SYMBOL  = "INDIAVIX"   # docs.openalgo.in/symbol-format

DAY_NAMES = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
MONTH_NAMES = {
    1:"January",   2:"February", 3:"March",    4:"April",
    5:"May",       6:"June",     7:"July",      8:"August",
    9:"September", 10:"October", 11:"November", 12:"December",
}

# Monitor job concurrency guard — prevents overlapping monitor ticks
_monitor_lock = threading.Lock()


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
    "symbol_ce"        : "",      # e.g. NIFTY19MAR2623000CE
    "symbol_pe"        : "",      # e.g. NIFTY19MAR2623000PE

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
    "entry_time"       : None,    # ISO string (JSON-serialisable)
    "entry_date"       : None,    # YYYY-MM-DD (stale-state detection on restart)

    # ── Margin info captured at entry (v4.0.0) ────────────────────────────────
    "margin_required"  : 0.0,     # From margin guard check
    "margin_available" : 0.0,     # From funds check (cash + collateral)

    # ── Running P&L (updated every monitor cycle) ────────────────────────────
    "today_pnl"        : 0.0,    # closed_pnl + current open_mtm

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

        state_dir  = os.path.dirname(os.path.abspath(STATE_FILE)) or "."
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

def nearest_tuesday_expiry() -> str:
    """
    Return nearest NIFTY weekly expiry (TUESDAY) as DDMMMYY.

    ⚠️  IMPORTANT — NSE EXPIRY CHANGE effective September 2, 2025:
        NIFTY 50 weekly & monthly options now expire every TUESDAY.
        (Previously Thursday — changed as part of SEBI's F&O restructuring.)
        Bank Nifty: Wednesday | Sensex/Bankex: Thursday

    Logic:
      • If today IS Tuesday and before market close (15:30 IST) → use today
      • If today IS Tuesday and after 15:30 → roll to NEXT Tuesday
      • Any other day → roll to nearest upcoming Tuesday
    """
    today      = date.today()
    now        = now_ist()
    # Tuesday = weekday 1
    days_ahead = (1 - today.weekday()) % 7   # 0 if today is Tuesday
    if days_ahead == 0 and now.hour >= 15 and now.minute >= 30:
        days_ahead = 7   # today's expiry already settled, use next week
    expiry = today + timedelta(days=days_ahead)
    result = expiry.strftime("%d%b%y").upper()
    pinfo(f"Auto expiry: {result}  (date: {expiry}, {expiry.strftime('%A')})")
    return result

def get_expiry() -> str:
    if AUTO_EXPIRY:
        return nearest_tuesday_expiry()
    pinfo(f"Manual expiry: {MANUAL_EXPIRY}")
    return MANUAL_EXPIRY


# ═══════════════════════════════════════════════════════════════════════════════
#  VIX FETCH  (primary: OpenAlgo  |  fallback: NSE direct API)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_vix() -> float:
    """
    Fetch India VIX LTP. Returns float > 0, or -1.0 on total failure.
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

    # Fallback: NSE direct API
    try:
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept"    : "application/json",
            "Referer"   : "https://www.nseindia.com",
        }
        sess = requests.Session()
        sess.get("https://www.nseindia.com", headers=hdrs, timeout=5)
        r = sess.get("https://www.nseindia.com/api/allIndices", headers=hdrs, timeout=6)
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
#  DAY + MONTH FILTERS
# ═══════════════════════════════════════════════════════════════════════════════

def trade_day_ok() -> bool:
    """Return True if today passes both day-of-week and month filters."""
    now     = now_ist()
    weekday = now.weekday()
    month   = now.month

    if weekday not in TRADE_DAYS:
        pinfo(f"Today is {DAY_NAMES[weekday]} — not in TRADE_DAYS {TRADE_DAYS}, skipping")
        return False
    if month in SKIP_MONTHS:
        pinfo(f"{MONTH_NAMES[month]} is in SKIP_MONTHS — skipping")
        telegram(f"Skipping — {MONTH_NAMES[month]} is a configured skip month")
        return False

    pinfo(f"Trade day OK: {DAY_NAMES[weekday]}, {MONTH_NAMES[month]} {now.year}")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
#  PRE-TRADE MARGIN GUARD  (NEW in v4.0.0)
#
#  OpenAlgo APIs used:
#    /api/v1/funds  → availablecash + collateral (pledged securities, haircut applied)
#    /api/v1/margin → basket margin for SELL CE + SELL PE MIS
#                     returns total_margin_required with SPAN straddle offset
#
#  Design decisions:
#    • FAIL-OPEN: if either API call fails, log + proceed (don't block live trades)
#    • Collateral is included: pledged Nifty BeES / liquid funds count as margin
#    • MARGIN_BUFFER = 1.20: 20% headroom handles intraday SPAN spikes
#    • Dhan note: margin() calculates legs sequentially (no basket offset),
#      so the 20% buffer also compensates for this conservatism
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
      availablecash + collateral = total available for trading
      utiliseddebits = already blocked (existing positions)

    Step 2: GET basket margin via client.margin()
      Sends both legs (CE + PE SELL MIS) to get combined straddle margin.
      Many brokers apply SPAN straddle portfolio offset — cheaper than 2 naked shorts.

    Step 3: Sufficiency check
      total_available >= required_margin × MARGIN_BUFFER

    Returns:
      True  — sufficient margin, proceed with entry
      False — insufficient margin, skip today's trade
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
            available_cash = float(data.get("availablecash", 0) or 0)
            collateral     = float(data.get("collateral",    0) or 0)
            utilised       = float(data.get("utiliseddebits",0) or 0)
            pinfo(f"  Available cash  : Rs.{available_cash:,.2f}")
            pinfo(f"  Collateral      : Rs.{collateral:,.2f}  (pledged securities)")
            pinfo(f"  Utilised debits : Rs.{utilised:,.2f}  (existing margin)")
        else:
            msg = funds_resp.get("message","") if isinstance(funds_resp, dict) else str(funds_resp)
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

    # Build approximate symbol strings for margin API call
    # OpenAlgo symbol format: NIFTY19MAR2623000CE
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
            span_margin     = float(margin_data.get("span_margin",  0) or 0)
            exposure_margin = float(margin_data.get("exposure_margin", 0) or 0)
            pinfo(f"  SPAN margin     : Rs.{span_margin:,.2f}")
            pinfo(f"  Exposure margin : Rs.{exposure_margin:,.2f}")
            pinfo(f"  Required total  : Rs.{required_margin:,.2f}")
        else:
            msg = margin_resp.get("message","") if isinstance(margin_resp, dict) else str(margin_resp)
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
            f"Required Rs.{required_margin:,.0f} (+{int((MARGIN_BUFFER-1)*100)}% = Rs.{required_with_buffer:,.0f})  |  "
            f"Surplus Rs.{surplus_or_shortfall:,.0f}"
        )
        psep()
        return True
    else:
        pwarn(
            f"  MARGIN CHECK: FAIL ✗  "
            f"Available Rs.{total_available:,.0f}  |  "
            f"Required Rs.{required_margin:,.0f} (+{int((MARGIN_BUFFER-1)*100)}% = Rs.{required_with_buffer:,.0f})  |  "
            f"Shortfall Rs.{abs(surplus_or_shortfall):,.0f}"
        )
        psep()
        telegram(
            f"⚠️ MARGIN INSUFFICIENT — trade SKIPPED\n"
            f"Available  : Rs.{total_available:,.0f}\n"
            f"  Cash     : Rs.{available_cash:,.0f}\n"
            f"  Collateral: Rs.{collateral:,.0f}\n"
            f"Required   : Rs.{required_margin:,.0f}\n"
            f"  +{int((MARGIN_BUFFER-1)*100)}% buffer = Rs.{required_with_buffer:,.0f}\n"
            f"Shortfall  : Rs.{abs(surplus_or_shortfall):,.0f}\n"
            f"Action: Add funds or reduce NUMBER_OF_LOTS."
        )
        return False


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY — Short ATM Straddle (SELL CE + SELL PE)
# ═══════════════════════════════════════════════════════════════════════════════

def place_entry() -> bool:
    """
    Place both legs atomically via optionsmultiorder().

    Success path:
      • Both legs filled → ce_active=True, pe_active=True
      • Fill prices captured via orderstatus() with retry
      • Full state saved to disk atomically
      • Returns True

    Failure paths:
      • API exception / rejected → Returns False, nothing opened
      • One leg filled, other failed (partial fill) → emergency close, Returns False
    """
    expiry = get_expiry()

    psep()
    pinfo("PLACING ENTRY — Short ATM Straddle  [PARTIAL SQUARE OFF]")
    pinfo(f"  Underlying  : {UNDERLYING}  |  Exchange  : {EXCHANGE}")
    pinfo(f"  Expiry      : {expiry}  |  Offset : {STRIKE_OFFSET}")
    pinfo(f"  Product     : {PRODUCT}  |  Qty/leg : {qty()}")
    pinfo(f"  CE SL will  = entry_price_CE × {1 + LEG_SL_PERCENT/100:.2f}")
    pinfo(f"  PE SL will  = entry_price_PE × {1 + LEG_SL_PERCENT/100:.2f}")
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
                f"| mode: {leg.get('mode','live').upper()}"
            )
        else:
            perr(f"  LEG {opt} FAILED: {leg.get('message', 'Unknown error')}")

    # ── Partial entry fill → emergency close ─────────────────────────────────
    if "CE" not in filled_legs or "PE" not in filled_legs:
        perr("PARTIAL ENTRY FILL — only one leg placed. Emergency close triggered.")
        telegram("PARTIAL ENTRY FILL — emergency close triggered. Check logs.")
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
    pinfo(f"  CE        : {state['symbol_ce']}  fill Rs.{state['entry_price_ce']:.2f}  SL @ Rs.{sl_ce:.2f}")
    pinfo(f"  PE        : {state['symbol_pe']}  fill Rs.{state['entry_price_pe']:.2f}  SL @ Rs.{sl_pe:.2f}")
    pinfo(f"  Margin used : Rs.{state['margin_required']:,.0f}  |  Available was: Rs.{state['margin_available']:,.0f}")
    pinfo(f"  State persisted → {STATE_FILE}")
    psep()

    telegram(
        f"✅ ENTRY PLACED [{trade_mode}]\n"
        f"NIFTY: {state['underlying_ltp']}  VIX: {state['vix_at_entry']:.2f}\n"
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

    Retry logic: up to 3 attempts with 1s delay.
    Accounts for broker API propagation delay after order placement.

    Failure handling:
      • After 3 retries, warns and leaves entry_price at 0.0
      • Position is already placed — we do NOT abort or unwind
      • SL check is skipped for that leg if entry_price is 0.0
    """
    MAX_ATTEMPTS = 3
    RETRY_DELAY  = 1.0   # seconds

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
                    msg = resp.get("message","") if isinstance(resp, dict) else str(resp)
                    pdebug(f"  orderstatus [{leg}] attempt {attempt} failed: {msg}")
            except Exception as exc:
                pdebug(f"  orderstatus [{leg}] attempt {attempt} exception: {exc}")

            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY)

        if not filled:
            pwarn(
                f"  Fill price [{leg}] unavailable after {MAX_ATTEMPTS} attempts "
                f"— SL disabled for this leg. Monitor will warn on each cycle."
            )


# ═══════════════════════════════════════════════════════════════════════════════
#  SINGLE-LEG CLOSE  ← CORE OF PARTIAL SQUARE OFF
#
#  When ONE leg hits its SL, ONLY that leg is closed.
#  The other leg continues running with its own SL intact.
#
#  What this function does:
#    1. Guard against double-close (idempotent)
#    2. Place BUY MARKET order for this leg only (reverses the SELL)
#    3. On success  → mark leg inactive, add approx P&L to closed_pnl, save state
#    4. If other leg still active → log partial state, save, return
#    5. If both legs now closed   → call _mark_fully_flat()
#    6. On order failure → log error + Telegram alert, RETURN without state change
#       (position still OPEN — operator must intervene)
# ═══════════════════════════════════════════════════════════════════════════════

def close_one_leg(leg: str, reason: str, current_ltp: float = 0.0):
    """
    Close a single leg (leg = 'CE' or 'PE').

    Parameters
    ----------
    leg         : 'CE' or 'PE'
    reason      : Human-readable close reason for logs and Telegram
    current_ltp : Last known LTP for approximate P&L estimate.
                  Actual fill may differ due to slippage (MARKET order).
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
            action    = "BUY",       # BUY reverses the short SELL
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
        state["in_position"] = True   # still in position
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

    Strategy:
      BOTH legs active → closeposition() (atomic, single API call)
      ONE leg active   → close_one_leg()  (targeted)
      NO legs active   → no-op
    """
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
        # Both legs open — use closeposition() for atomic close
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
            state["ce_active"] = False
            state["pe_active"] = False
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
        # Only one leg remaining — close it individually
        leg = active[0]
        pinfo(f"Only {leg} active → close_one_leg({leg})")
        close_one_leg(leg, reason=reason)


def _emergency_close_all():
    """
    Best-effort close for emergency scenarios (partial entry fill, orphan positions).
    Uses closeposition(). Does NOT update state.
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
    """Reset all position fields, delete state file, send final summary."""
    final_pnl    = state["today_pnl"]
    duration_str = ""

    if state.get("entry_time"):
        try:
            entry_dt = datetime.fromisoformat(state["entry_time"])
            if entry_dt.tzinfo is None:
                entry_dt = entry_dt.replace(tzinfo=IST)
            held_mins    = int((now_ist() - entry_dt).total_seconds() // 60)
            duration_str = f"  |  Held: {held_mins} min"
        except Exception:
            pass

    # Reset all position-related state
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
    state["underlying_ltp"]   = 0.0
    state["vix_at_entry"]     = 0.0
    state["entry_time"]       = None
    state["entry_date"]       = None
    state["margin_required"]  = 0.0
    state["margin_available"] = 0.0
    state["exit_reason"]      = reason
    state["trade_count"]     += 1

    # Delete state file — flat position needs no persistence
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
#  (APScheduler may overlap if a tick takes > MONITOR_INTERVAL_S seconds)
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
        pwarn(f"quotes() failed [{leg}]: {q.get('message','') if isinstance(q,dict) else str(q)}")
        return 0.0
    except Exception as exc:
        pwarn(f"quotes() exception [{leg}]: {exc}")
        return 0.0


def monitor_pnl():
    """
    Monitor tick — runs every MONITOR_INTERVAL_S seconds.
    Implements full partial square off logic.
    Protected by threading lock to prevent overlapping executions.
    """
    if not state["in_position"]:
        return

    # Non-blocking lock: skip this tick if previous one is still running
    acquired = _monitor_lock.acquire(blocking=False)
    if not acquired:
        pwarn("Monitor tick skipped — previous tick still running (lock contention)")
        return

    try:
        _run_monitor_tick()
    finally:
        _monitor_lock.release()


def _run_monitor_tick():
    """Inner monitor logic — called from monitor_pnl() under lock."""
    open_mtm = 0.0   # sum of live MTM for still-open legs only

    for leg in ["CE", "PE"]:
        active_key = f"{leg.lower()}_active"

        # Skip legs already closed
        if not state[active_key]:
            continue

        entry_px = state[f"entry_price_{leg.lower()}"]
        sl_lvl   = sl_level(leg)
        ltp      = _fetch_ltp(leg)

        # Skip this leg if LTP fetch failed — do NOT fire SL on bad data
        if ltp <= 0:
            pwarn(f"LTP unavailable for {leg} this cycle — skipping SL check")
            continue

        leg_mtm = (entry_px - ltp) * qty() if entry_px > 0 else 0.0

        pdebug(
            f"  {leg} | entry Rs.{entry_px:.2f} | ltp Rs.{ltp:.2f} | "
            f"mtm Rs.{leg_mtm:.0f} | sl @ Rs.{sl_lvl:.2f}"
        )

        # ── PER-LEG SL CHECK ─────────────────────────────────────────────────
        # Only fires if: SL is enabled, entry price was captured, LTP breached SL
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

        # ── No SL — accumulate to open MTM ───────────────────────────────────
        open_mtm += leg_mtm

    # ── If both legs closed by SL(s) this cycle → already flat, done ─────────
    if not state["in_position"]:
        return

    # ── Combined P&L = closed legs (realised) + open legs (unrealised) ───────
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

    today_str    = date.today().isoformat()
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
        pinfo("Case B: Saved position + broker confirms open positions → RESTORING STATE")

        # Restore every saved key into live state
        for key in state:
            if key in saved:
                state[key] = saved[key]

        # Re-parse ISO string back to datetime
        if isinstance(state.get("entry_time"), str):
            try:
                state["entry_time"] = datetime.fromisoformat(
                    state["entry_time"]
                ).replace(tzinfo=IST)
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
            f"CE: {saved.get('symbol_ce','?')}\n"
            f"PE: {saved.get('symbol_pe','?')}\n"
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
    Fetch open NFO positions from broker via positionbook().
    Returns list of position dicts with non-zero qty on NFO.
    Returns [] on any failure.
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
    Entry job — fires once at ENTRY_TIME on configured TRADE_DAYS.

    Filter order (short-circuit on first failure):
      1. Duplicate guard (already in position)
      2. Trade day filter (day of week + skip months)
      3. VIX filter (VIX_MIN ≤ VIX ≤ VIX_MAX)
      4. Margin guard (available capital ≥ required × buffer)  ← NEW v4.0.0
      5. Place straddle entry
    """
    psep()
    pinfo(f"ENTRY JOB | {now_ist().strftime('%A %d-%b-%Y %H:%M:%S IST')}")
    psep()

    # ── 1. Duplicate guard ────────────────────────────────────────────────────
    if state["in_position"]:
        pwarn("Already in position — entry skipped (duplicate guard)")
        return

    # ── 2. Trade day filter ───────────────────────────────────────────────────
    if not trade_day_ok():
        return

    # ── 3. VIX filter ─────────────────────────────────────────────────────────
    if not vix_ok():
        return

    # ── 4. Pre-trade margin guard ─────────────────────────────────────────────
    expiry = get_expiry()
    if not check_margin_sufficient(expiry):
        perr("Entry ABORTED — insufficient margin (cash + collateral)")
        return

    # ── 5. Reset daily counters and place entry ───────────────────────────────
    state["today_pnl"]  = 0.0
    state["closed_pnl"] = 0.0

    success = place_entry()
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


# ═══════════════════════════════════════════════════════════════════════════════
#  STARTUP BANNER
# ═══════════════════════════════════════════════════════════════════════════════

def _print_banner():
    days_str  = ", ".join(DAY_NAMES[d] for d in sorted(TRADE_DAYS))
    skip_str  = ", ".join(MONTH_NAMES[m] for m in sorted(SKIP_MONTHS)) if SKIP_MONTHS else "None"
    guard_str = (
        f"ENABLED  buffer={int((MARGIN_BUFFER-1)*100)}%  "
        f"fail_open={MARGIN_GUARD_FAIL_OPEN}"
        if MARGIN_GUARD_ENABLED else "DISABLED"
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
    print(f"  Entry            : {ENTRY_TIME} IST  |  Hard exit : {EXIT_TIME} IST", flush=True)
    print(f"  Monitor interval : every {MONITOR_INTERVAL_S}s", flush=True)
    print(f"  Trade days       : {days_str}", flush=True)
    print(f"  Skip months      : {skip_str}", flush=True)
    print(f"  VIX filter       : {VIX_MIN}–{VIX_MAX}  (enabled: {VIX_FILTER_ENABLED})", flush=True)
    print(f"  Sq-off mode      : PARTIAL — each leg has independent {LEG_SL_PERCENT}% SL", flush=True)
    print(f"  Daily target     : Rs.{DAILY_PROFIT_TARGET}  (combined, 0=disabled)", flush=True)
    print(f"  Daily limit      : Rs.{DAILY_LOSS_LIMIT}   (combined, 0=disabled)", flush=True)
    print(f"  Margin guard     : {guard_str}", flush=True)
    print(f"  Auto expiry      : {AUTO_EXPIRY}  |  Manual : {MANUAL_EXPIRY}  (NIFTY expires TUESDAY)", flush=True)
    print(f"  State file       : {os.path.abspath(STATE_FILE)}", flush=True)
    print(f"  Telegram         : {TELEGRAM_ENABLED}", flush=True)
    print("=" * 72, flush=True)
    print(f"  Backtest (2019-2026): P&L Rs.5,04,192 | Win 66.71% | MaxDD Rs.34,179", flush=True)
    print(f"  Avg/trade Rs.289 | Return/MDD 1.38 | 1746 trades", flush=True)
    print("─" * 72, flush=True)
    print(f"  Partial logic: each leg has its OWN 20% SL.", flush=True)
    print(f"  SL on CE → close CE only. PE continues with its own SL.", flush=True)
    print(f"  SL on PE → close PE only. CE continues with its own SL.", flush=True)
    print(f"  combined P&L = closed_pnl + open_leg_mtm", flush=True)
    print(f"  Margin guard: funds() + margin() checked before every entry.", flush=True)
    print(f"  Analyze Mode (paper/live) is set in the OpenAlgo dashboard.", flush=True)
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
            data  = resp.get("data", {})
            cash  = float(data.get("availablecash",  0) or 0)
            coll  = float(data.get("collateral",     0) or 0)
            used  = float(data.get("utiliseddebits", 0) or 0)
            m2m   = float(data.get("m2munrealized",  0) or 0)
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
    """Print full in-memory state + computed SL levels to stdout."""
    psep()
    pinfo("STATE DUMP:")
    for k, v in state.items():
        print(f"    {k:<24} : {v}", flush=True)
    print(f"    {'sl_ce (computed)':<24} : Rs.{sl_level('CE'):.2f}", flush=True)
    print(f"    {'sl_pe (computed)':<24} : Rs.{sl_level('PE'):.2f}", flush=True)
    print(f"    {'active_legs':<24} : {active_legs()}", flush=True)
    psep()

def check_margin_now():
    """
    Manual margin check — call this to test margin guard without placing a trade.
    Useful for verifying API connectivity and capital sufficiency.
    """
    expiry = get_expiry()
    result = check_margin_sufficient(expiry)
    pinfo(f"Margin check result: {'PASS ✓' if result else 'FAIL ✗'}")


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    """
    Production startup:
      1. Print banner
      2. Test connection + show funds (including collateral)
      3. Reconcile state with broker (restart-safe)
      4. Start APScheduler (entry / exit / monitor)
      5. Graceful shutdown on Ctrl+C or crash
    """
    _print_banner()
    check_connection()
    reconcile_on_startup()

    entry_h, entry_m = parse_hhmm(ENTRY_TIME)
    exit_h,  exit_m  = parse_hhmm(EXIT_TIME)

    scheduler = BlockingScheduler(timezone=IST)

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

    pinfo(
        f"Scheduler running | Entry: {ENTRY_TIME} | "
        f"Exit: {EXIT_TIME} | Monitor: every {MONITOR_INTERVAL_S}s"
    )
    pinfo("Press Ctrl+C to stop gracefully")
    print("", flush=True)

    guard_status = (
        f"Margin guard: ENABLED ({int((MARGIN_BUFFER-1)*100)}% buffer, "
        f"fail_open={MARGIN_GUARD_FAIL_OPEN})"
        if MARGIN_GUARD_ENABLED else "Margin guard: DISABLED"
    )
    telegram(
        f"🚀 Strategy STARTED v{VERSION} [PARTIAL]\n"
        f"Entry: {ENTRY_TIME}  Hard Exit: {EXIT_TIME}\n"
        f"Qty/leg: {NUMBER_OF_LOTS}×{LOT_SIZE} = {qty()}\n"
        f"Leg SL: {LEG_SL_PERCENT}% (independent per leg)\n"
        f"VIX: {VIX_MIN}–{VIX_MAX}\n"
        f"Days: {', '.join(DAY_NAMES[d][:3] for d in sorted(TRADE_DAYS))}\n"
        f"Target: Rs.{DAILY_PROFIT_TARGET}  Limit: Rs.{DAILY_LOSS_LIMIT}\n"
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
      show_state()         → dump full state + computed SL levels
    """

    # ── Production ────────────────────────────────────────────────────────────
    run()

    # ── Testing (uncomment one at a time) ─────────────────────────────────────
    # check_connection()
    # check_margin_now()
    # manual_entry()
    # manual_exit()
    # show_state()