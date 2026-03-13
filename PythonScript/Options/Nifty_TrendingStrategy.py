"""
╔══════════════════════════════════════════════════════════════════════════════╗
║        NIFTY TRENDING STRATEGY  —  OpenAlgo + Dhan                        ║
║        Short ATM Straddle  |  Weekly Expiry  |  Intraday MIS              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Backtest  (AlgoTest 2019–2026  |  793 trades)                            ║
║  P&L: Rs.2,35,610  |  Win: 53.85%  |  MaxDD: Rs.35,097  |  Avg: Rs.297  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  RESTART SAFE                                                               ║
║  State is persisted to strategy_state.json after every change.             ║
║  On startup, script reconciles with live broker positions via              ║
║  positionbook() — restores state if a position is already open,           ║
║  or clears stale state if broker shows flat. Safe to restart anytime.     ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  QUICK START                                                                ║
║  1. pip install openalgo apscheduler pytz                                  ║
║  2. Fill OPENALGO_API_KEY and OPENALGO_HOST in SECTION 1                  ║
║  3. Enable Analyze Mode in OpenAlgo dashboard (paper trade first)          ║
║  4. python trending_strategy_openalgo.py                                   ║
║  5. Once satisfied → disable Analyze Mode in dashboard → goes LIVE        ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  SYMBOL FORMAT  (docs.openalgo.in/symbol-format)                          ║
║  Options  : NIFTY28MAR2523000CE  →  [BASE][DDMMMYY][STRIKE][CE/PE]       ║
║  F&O exch : NFO   → for quotes/positions on option contracts               ║
║  Index    : NSE_INDEX  → for order entry (underlying) and VIX fetch       ║
║  VIX      : symbol=INDIAVIX, exchange=NSE_INDEX                           ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""
import os
# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 1 — OPENALGO CONNECTION
# ═══════════════════════════════════════════════════════════════════════════════

OPENALGO_HOST    = "http://127.0.0.1:5000"      # Local or ngrok domain URL
OPENALGO_API_KEY = os.getenv("OPENALGO_APIKEY")  # OpenAlgo Dashboard → API Keys

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 2 — INSTRUMENT SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════

UNDERLYING     = "NIFTY"       # NIFTY | BANKNIFTY | FINNIFTY
EXCHANGE       = "NSE_INDEX"   # Always NSE_INDEX for index-based option orders
LOT_SIZE       = 75            # NIFTY=75 | BANKNIFTY=30 | FINNIFTY=40
NUMBER_OF_LOTS = 1             # Lots per leg — increase only after stable paper trading
PRODUCT        = "MIS"         # MIS=intraday auto sq-off | NRML=overnight carry
STRIKE_OFFSET  = "ATM"         # ATM=straddle | OTM1..OTM50 | ITM1..ITM50

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 3 — TIMING  (IST 24-hour HH:MM)
#  Backtest config: Entry 09:17 | Exit 15:15
# ═══════════════════════════════════════════════════════════════════════════════

ENTRY_TIME         = "09:17"  # When to place the short straddle
EXIT_TIME          = "15:15"  # Hard square-off (always runs, regardless of P&L)
MONITOR_INTERVAL_S = 60       # Seconds between P&L checks

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 4 — TRADE DAY FILTER
#  7-year backtest (793 trades):
#    Thu Rs.2,07,418 (88% of profit!)  |  Wed Rs.1,00,311  |  Fri Rs.92,238
#    Tue Rs.56,583 (avg Rs.161)        |  Mon Rs.49,550 (avg Rs.143)
#
#  0=Mon 1=Tue 2=Wed 3=Thu 4=Fri
# ═══════════════════════════════════════════════════════════════════════════════

TRADE_DAYS = [2, 3, 4]           # Wed+Thu+Fri — best risk-adjusted days
# TRADE_DAYS = [3]               # Thursday only — highest single-day profit
# TRADE_DAYS = [0, 1, 2, 3, 4]  # All weekdays

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 5 — MONTH FILTER
#  Nov → Rs.-6,627 (ONLY loss month — always skip)
#  Apr → Rs.25,812 (lowest, 45.8% win — optional skip)
#
#  1=Jan 2=Feb 3=Mar 4=Apr 5=May 6=Jun 7=Jul 8=Aug 9=Sep 10=Oct 11=Nov 12=Dec
# ═══════════════════════════════════════════════════════════════════════════════

SKIP_MONTHS = [11]               # Skip November
# SKIP_MONTHS = [4, 11]          # Conservative — skip April too
# SKIP_MONTHS = []               # Trade all months

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 6 — VIX FILTER
#  < 14   : Thin premiums — not worth selling
#  14–20  : Sweet spot — best premium/risk balance
#  20–25  : Tradeable — expect more SL hits
#  25–28  : Be cautious — reduce lots
#  > 28   : Danger zone — skip (COVID VIX was 80+)
#
#  Symbol: INDIAVIX on NSE_INDEX (confirmed docs.openalgo.in/symbol-format)
# ═══════════════════════════════════════════════════════════════════════════════

VIX_FILTER_ENABLED = True    # False = bypass VIX check entirely
VIX_MIN            = 14.0    # Skip trade if VIX below this
VIX_MAX            = 28.0    # Skip trade if VIX above this

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 7 — RISK MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

LEG_SL_PERCENT      = 20.0   # Per-leg SL as % of entry premium (0 = disabled)
DAILY_PROFIT_TARGET = 5000   # Close all if total MTM profit reaches this (0 = disabled)
DAILY_LOSS_LIMIT    = -4000  # Close all if total MTM loss hits this — use negative (0 = disabled)

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 8 — EXPIRY CONFIG
#  Format confirmed from OpenAlgo SDK docs: DDMMMYY uppercase
#  Examples: "27MAR25", "28OCT25", "25NOV25", "30DEC25"
# ═══════════════════════════════════════════════════════════════════════════════

AUTO_EXPIRY   = True       # True = auto nearest Thursday. False = use MANUAL_EXPIRY.
MANUAL_EXPIRY = "27MAR25"  # Used only when AUTO_EXPIRY = False

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 9 — STRATEGY NAME
#  Must match exactly what is registered in OpenAlgo dashboard.
# ═══════════════════════════════════════════════════════════════════════════════

STRATEGY_NAME = "Nifty Trending Straddle"

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 10 — TELEGRAM ALERTS  (optional)
#  BOT_TOKEN: from @BotFather  |  CHAT_ID: from @userinfobot
# ═══════════════════════════════════════════════════════════════════════════════

TELEGRAM_ENABLED   = False
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# ═══════════════════════════════════════════════════════════════════════════════
#  SECTION 11 — STATE PERSISTENCE FILE
#  Saves strategy state to this JSON file after every state change.
#  On restart, the script reads this file and reconciles with live broker
#  positions before starting the scheduler.
#  Use absolute path if running as a service.
# ═══════════════════════════════════════════════════════════════════════════════

STATE_FILE = "strategy_state.json"   # Created in script's working directory

# ═══════════════════════════════════════════════════════════════════════════════
#  END OF CONFIGURATION — do not edit below unless extending strategy logic
# ═══════════════════════════════════════════════════════════════════════════════


import sys
import json
import time
import requests
import pytz
from datetime import datetime, date, timedelta
from openalgo import api as OpenAlgoClient
from apscheduler.schedulers.blocking import BlockingScheduler


# ───────────────────────────────────────────────────────────────────────────────
#  Internal constants
# ───────────────────────────────────────────────────────────────────────────────

VERSION     = "2.0.0"
IST         = pytz.timezone("Asia/Kolkata")
OPTION_EXCH = "NFO"       # All F&O contracts use NFO for quotes/positions
INDEX_EXCH  = "NSE_INDEX" # Underlying index (NIFTY, INDIAVIX) uses NSE_INDEX
VIX_SYMBOL  = "INDIAVIX"  # Confirmed: docs.openalgo.in/symbol-format

DAY_NAMES   = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
MONTH_NAMES = {
    1:"January", 2:"February", 3:"March",    4:"April",
    5:"May",     6:"June",     7:"July",      8:"August",
    9:"September",10:"October",11:"November",12:"December"
}

# ───────────────────────────────────────────────────────────────────────────────
#  In-memory state (also persisted to STATE_FILE after every mutation)
# ───────────────────────────────────────────────────────────────────────────────

state = {
    "in_position"    : False,
    "symbol_ce"      : "",      # e.g. NIFTY27MAR2523000CE — resolved by OpenAlgo
    "symbol_pe"      : "",      # e.g. NIFTY27MAR2523000PE
    "orderid_ce"     : "",
    "orderid_pe"     : "",
    "entry_price_ce" : 0.0,     # Average fill price for CE leg
    "entry_price_pe" : 0.0,     # Average fill price for PE leg
    "underlying_ltp" : 0.0,     # NIFTY spot at entry
    "vix_at_entry"   : 0.0,     # India VIX at entry
    "entry_time"     : None,    # ISO string (serialisable) — NOT datetime object
    "entry_date"     : None,    # YYYY-MM-DD — used to detect stale state next day
    "today_pnl"      : 0.0,     # Running MTM P&L
    "trade_count"    : 0,       # Trades completed this session
    "exit_reason"    : "",      # Reason for last exit
}

# ───────────────────────────────────────────────────────────────────────────────
#  OpenAlgo SDK client
# ───────────────────────────────────────────────────────────────────────────────

client = OpenAlgoClient(api_key=OPENALGO_API_KEY, host=OPENALGO_HOST)


# ═══════════════════════════════════════════════════════════════════════════════
#  PRINT-BASED LOGGER
#  All output via print(flush=True) — captured by OpenAlgo's built-in log system
#  Format: [YYYY-MM-DD HH:MM:SS] [LEVEL   ] message
# ═══════════════════════════════════════════════════════════════════════════════

def now_ist() -> datetime:
    return datetime.now(IST)

def ts() -> str:
    return f"[{now_ist().strftime('%Y-%m-%d %H:%M:%S')}]"

def plog(level: str, msg: str):
    print(f"{ts()} [{level:<8}] {msg}", flush=True)

def pinfo(msg):  plog("INFO",    msg)
def pwarn(msg):  plog("WARNING", msg)
def perr(msg):   plog("ERROR",   msg)
def pdebug(msg): plog("DEBUG",   msg)

def psep():
    print(f"{ts()} {'─' * 62}", flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  STATE PERSISTENCE
#  Saves state dict to JSON after every mutation.
#  entry_time stored as ISO string for JSON serialisability.
# ═══════════════════════════════════════════════════════════════════════════════

def save_state():
    """Persist current state to STATE_FILE (JSON). Called after every state change."""
    try:
        payload = dict(state)
        # datetime objects are not JSON serialisable — store as ISO string
        if isinstance(payload.get("entry_time"), datetime):
            payload["entry_time"] = payload["entry_time"].isoformat()
        with open(STATE_FILE, "w") as f:
            json.dump(payload, f, indent=2)
        pdebug(f"State saved to {STATE_FILE}")
    except Exception as e:
        pwarn(f"Could not save state to {STATE_FILE}: {e}")


def load_state() -> dict:
    """
    Load persisted state from STATE_FILE.
    Returns the loaded dict, or an empty dict if file doesn't exist or is corrupt.
    """
    if not os.path.exists(STATE_FILE):
        pinfo(f"No state file found at {STATE_FILE} — starting fresh")
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            loaded = json.load(f)
        pinfo(f"State file loaded from {STATE_FILE}")
        return loaded
    except Exception as e:
        pwarn(f"Could not read state file {STATE_FILE}: {e} — starting fresh")
        return {}


def clear_state_file():
    """Delete STATE_FILE. Called after confirmed flat position."""
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
            pinfo(f"State file cleared ({STATE_FILE})")
    except Exception as e:
        pwarn(f"Could not delete state file: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  STARTUP RECONCILIATION
#  On every startup (restart or fresh start), this function:
#  1. Loads persisted state from STATE_FILE
#  2. Fetches live open positions from OpenAlgo positionbook()
#  3. Decides one of four outcomes:
#
#  Case A — No state file, broker flat         → fresh start, no action needed
#  Case B — State says IN POSITION, broker confirms open positions
#           → restore state, monitor resumes normally
#  Case C — State says IN POSITION, broker shows FLAT
#           → position was closed externally (broker SQ-OFF, manual close)
#             → clear stale state, log warning
#  Case D — No state file, but broker shows NFO positions open
#           → unexpected open position (orphan)
#             → log critical warning, attempt emergency close
# ═══════════════════════════════════════════════════════════════════════════════

def reconcile_on_startup():
    """
    Reconcile persisted state with live broker positions on every startup.
    This makes the script completely safe to restart at any time.
    """
    psep()
    pinfo("STARTUP RECONCILIATION — checking state vs live broker positions")
    psep()

    # ── Step 1: Load saved state ──────────────────────────────────────────────
    saved = load_state()

    # ── Step 2: Fetch live positions from broker via OpenAlgo ─────────────────
    broker_positions = fetch_broker_positions()

    today_str   = date.today().isoformat()         # e.g. "2025-03-27"
    saved_date  = saved.get("entry_date", "")
    saved_in_pos = saved.get("in_position", False)

    # ── Step 3: Check if saved state is from a previous day ───────────────────
    if saved_in_pos and saved_date and saved_date != today_str:
        pwarn(f"Saved state is from {saved_date} (today is {today_str})")
        pwarn("Stale state from previous day — MIS positions would have been auto sq-off by broker")
        pwarn("Clearing stale state and starting fresh")
        state["in_position"] = False
        clear_state_file()
        telegram("RESTART: Stale state from previous day cleared. Starting fresh.")
        psep()
        return

    # ── Case A: No saved position, broker is flat ─────────────────────────────
    if not saved_in_pos and not broker_positions:
        pinfo("Case A: No saved position + broker flat → clean start")
        psep()
        return

    # ── Case B: State says IN POSITION, broker confirms ───────────────────────
    if saved_in_pos and broker_positions:
        pinfo("Case B: State shows open position + broker confirms open positions → RESTORING STATE")

        # Restore all saved state fields into live state dict
        for key in state:
            if key in saved:
                state[key] = saved[key]

        # entry_time was saved as ISO string — parse back to datetime
        if isinstance(state.get("entry_time"), str):
            try:
                state["entry_time"] = datetime.fromisoformat(state["entry_time"]).replace(tzinfo=IST)
            except Exception:
                state["entry_time"] = now_ist()

        pinfo(f"  CE Symbol : {state['symbol_ce']}")
        pinfo(f"  PE Symbol : {state['symbol_pe']}")
        pinfo(f"  CE Fill   : Rs.{state['entry_price_ce']:.2f}")
        pinfo(f"  PE Fill   : Rs.{state['entry_price_pe']:.2f}")
        pinfo(f"  Entry at  : {state['entry_time']}")
        pinfo("State restored — monitor will resume from next cycle")

        telegram(
            f"RESTARTED — position RESTORED\n"
            f"CE: {state['symbol_ce']} @ Rs.{state['entry_price_ce']:.2f}\n"
            f"PE: {state['symbol_pe']} @ Rs.{state['entry_price_pe']:.2f}\n"
            f"Monitoring resumed."
        )
        psep()
        return

    # ── Case C: State says IN POSITION, but broker is flat ────────────────────
    if saved_in_pos and not broker_positions:
        pwarn("Case C: State shows open position BUT broker is FLAT")
        pwarn("Position was likely closed externally (broker SQ-OFF, manual close, or broker restart)")
        pwarn("Clearing stale state — no action needed")

        state["in_position"] = False
        state["exit_reason"] = "Position closed externally before restart"
        clear_state_file()

        telegram(
            f"RESTART WARNING: State showed open position but broker is FLAT.\n"
            f"CE: {saved.get('symbol_ce', 'unknown')}\n"
            f"PE: {saved.get('symbol_pe', 'unknown')}\n"
            f"Position was closed externally. State cleared."
        )
        psep()
        return

    # ── Case D: No saved state, but broker shows NFO positions ────────────────
    if not saved_in_pos and broker_positions:
        perr("Case D: NO saved state but broker shows OPEN NFO POSITIONS")
        perr("These are ORPHAN positions — not tracked by this strategy")
        perr("Positions found:")
        for p in broker_positions:
            perr(f"  {p['symbol']} | qty: {p['quantity']} | avg: {p['average_price']}")
        perr("ACTION: Attempting emergency close of all positions")

        telegram(
            f"CRITICAL: Orphan NFO positions found on restart!\n"
            + "\n".join(f"{p['symbol']} qty:{p['quantity']}" for p in broker_positions)
            + f"\nAttempting emergency close."
        )

        try:
            resp = client.closeposition(strategy=STRATEGY_NAME)
            if isinstance(resp, dict) and resp.get("status") == "success":
                perr("Emergency close SUCCESS — orphan positions squared off")
                telegram("Emergency close SUCCESS — orphan positions squared off")
            else:
                perr(f"Emergency close FAILED: {resp}")
                perr("*** MANUAL ACTION REQUIRED in broker terminal ***")
                telegram("Emergency close FAILED — MANUAL ACTION REQUIRED")
        except Exception as e:
            perr(f"Emergency close exception: {e}")
            perr("*** MANUAL ACTION REQUIRED in broker terminal ***")

        psep()
        return

    psep()


def fetch_broker_positions() -> list:
    """
    Fetch open NFO positions from OpenAlgo positionbook().
    Returns a list of open position dicts (non-zero quantity on NFO).
    Returns empty list on error or no positions.
    """
    try:
        resp = client.positionbook()
        if not isinstance(resp, dict) or resp.get("status") != "success":
            pwarn(f"positionbook() failed: {resp}")
            return []

        positions = resp.get("data", [])
        # Filter for open NFO positions with non-zero quantity
        open_nfo = [
            p for p in positions
            if p.get("exchange", "") == OPTION_EXCH
            and int(p.get("quantity", 0)) != 0
        ]

        if open_nfo:
            pinfo(f"Broker shows {len(open_nfo)} open NFO position(s):")
            for p in open_nfo:
                pinfo(f"  {p.get('symbol')} | qty: {p.get('quantity')} | avg: {p.get('average_price')}")
        else:
            pinfo("Broker shows no open NFO positions")

        return open_nfo

    except Exception as e:
        pwarn(f"fetch_broker_positions exception: {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
#  STARTUP BANNER
# ═══════════════════════════════════════════════════════════════════════════════

def print_banner():
    days_str = ", ".join(DAY_NAMES[d] for d in sorted(TRADE_DAYS))
    skip_str = ", ".join(MONTH_NAMES[m] for m in sorted(SKIP_MONTHS)) if SKIP_MONTHS else "None"
    print("", flush=True)
    print("=" * 65, flush=True)
    print(f"  NIFTY TRENDING STRADDLE  v{VERSION}", flush=True)
    print(f"  OpenAlgo + Dhan API  |  Restart-Safe Production Strategy", flush=True)
    print("=" * 65, flush=True)
    print(f"  Host           : {OPENALGO_HOST}", flush=True)
    print(f"  Strategy       : {STRATEGY_NAME}", flush=True)
    print(f"  Underlying     : {UNDERLYING}  |  Exchange : {EXCHANGE}", flush=True)
    print(f"  Lot Size       : {LOT_SIZE}  |  Lots: {NUMBER_OF_LOTS}  |  Qty/leg: {qty()}", flush=True)
    print(f"  Strike Offset  : {STRIKE_OFFSET}  |  Product: {PRODUCT}", flush=True)
    print(f"  Entry          : {ENTRY_TIME} IST  |  Exit: {EXIT_TIME} IST", flush=True)
    print(f"  Monitor        : every {MONITOR_INTERVAL_S}s", flush=True)
    print(f"  Trade Days     : {days_str}", flush=True)
    print(f"  Skip Months    : {skip_str}", flush=True)
    print(f"  VIX Filter     : {VIX_MIN} – {VIX_MAX}  (enabled: {VIX_FILTER_ENABLED})", flush=True)
    print(f"  VIX Symbol     : {VIX_SYMBOL} on {INDEX_EXCH}", flush=True)
    print(f"  Leg SL         : {LEG_SL_PERCENT}%", flush=True)
    print(f"  Profit Target  : Rs.{DAILY_PROFIT_TARGET}  |  Loss Limit: Rs.{DAILY_LOSS_LIMIT}", flush=True)
    print(f"  Auto Expiry    : {AUTO_EXPIRY}  |  Manual: {MANUAL_EXPIRY}", flush=True)
    print(f"  State File     : {os.path.abspath(STATE_FILE)}", flush=True)
    print(f"  Telegram       : {TELEGRAM_ENABLED}", flush=True)
    print("=" * 65, flush=True)
    print(f"  Analyze Mode (paper/live) is set in the OpenAlgo dashboard.", flush=True)
    print("=" * 65, flush=True)
    print("", flush=True)


# ═══════════════════════════════════════════════════════════════════════════════
#  UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def qty() -> int:
    return NUMBER_OF_LOTS * LOT_SIZE

def parse_hhmm(t: str):
    h, m = t.strip().split(":")
    return int(h), int(m)


# ═══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM
# ═══════════════════════════════════════════════════════════════════════════════

def telegram(msg: str):
    if not TELEGRAM_ENABLED:
        return
    try:
        url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"[{STRATEGY_NAME}]\n{msg}", "parse_mode": "HTML"}
        r = requests.post(url, json=payload, timeout=5)
        if r.status_code != 200:
            pwarn(f"Telegram HTTP {r.status_code}")
    except Exception as e:
        pwarn(f"Telegram failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  EXPIRY CALCULATION
#  Format confirmed from OpenAlgo Python SDK: DDMMMYY uppercase
#  e.g. "27MAR25", "28OCT25", "25NOV25"
# ═══════════════════════════════════════════════════════════════════════════════

def nearest_thursday_expiry() -> str:
    """
    Return nearest NIFTY weekly expiry (Thursday) as DDMMMYY.
    If today is Thursday before 15:00 → use today.
    If today is Thursday at/after 15:00 → use next Thursday.
    """
    today      = date.today()
    days_ahead = (3 - today.weekday()) % 7   # 0 if today is Thursday
    if days_ahead == 0 and now_ist().hour >= 15:
        days_ahead = 7
    expiry_date = today + timedelta(days=days_ahead)
    result      = expiry_date.strftime("%d%b%y").upper()
    pinfo(f"Auto expiry: {result}  ({expiry_date})")
    return result

def get_expiry() -> str:
    if AUTO_EXPIRY:
        return nearest_thursday_expiry()
    pinfo(f"Manual expiry: {MANUAL_EXPIRY}")
    return MANUAL_EXPIRY


# ═══════════════════════════════════════════════════════════════════════════════
#  VIX FETCH
#  symbol=INDIAVIX (no space), exchange=NSE_INDEX
#  Confirmed: docs.openalgo.in/symbol-format → Common NSE Index Symbols
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_vix() -> float:
    """Fetch India VIX. Returns float or -1.0 on failure."""
    # Primary: OpenAlgo SDK
    try:
        resp = client.quotes(symbol=VIX_SYMBOL, exchange=INDEX_EXCH)
        if isinstance(resp, dict) and resp.get("status") == "success":
            ltp = float(resp.get("data", {}).get("ltp", -1))
            if ltp > 0:
                pinfo(f"India VIX (OpenAlgo): {ltp:.2f}")
                return ltp
    except Exception as e:
        pwarn(f"OpenAlgo VIX fetch exception: {e}")

    # Fallback: NSE direct API
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept"    : "application/json",
            "Referer"   : "https://www.nseindia.com",
        }
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=headers, timeout=5)
        r = session.get("https://www.nseindia.com/api/allIndices", headers=headers, timeout=6)
        for item in r.json().get("data", []):
            if item.get("index", "").replace(" ", "").upper() == "INDIAVIX":
                vix = float(item["last"])
                pinfo(f"India VIX (NSE fallback): {vix:.2f}")
                return vix
    except Exception as e:
        pwarn(f"NSE VIX fallback exception: {e}")

    perr("India VIX unavailable from all sources")
    return -1.0


def vix_ok() -> bool:
    if not VIX_FILTER_ENABLED:
        pinfo("VIX filter disabled — skipping VIX check")
        return True

    vix = fetch_vix()

    if vix < 0:
        pwarn("VIX fetch failed — skipping trade as precaution")
        telegram("VIX unavailable — no trade today")
        return False
    if vix < VIX_MIN:
        pwarn(f"VIX {vix:.1f} < {VIX_MIN} — premiums too thin, skipping")
        telegram(f"VIX {vix:.1f} below {VIX_MIN} — no trade today")
        return False
    if vix > VIX_MAX:
        pwarn(f"VIX {vix:.1f} > {VIX_MAX} — danger zone, skipping")
        telegram(f"VIX {vix:.1f} above {VIX_MAX} — DANGER, no trade today!")
        return False

    pinfo(f"VIX {vix:.1f} within [{VIX_MIN} – {VIX_MAX}] — OK to trade")
    state["vix_at_entry"] = vix
    return True


# ═══════════════════════════════════════════════════════════════════════════════
#  DAY + MONTH FILTERS
# ═══════════════════════════════════════════════════════════════════════════════

def trade_day_ok() -> bool:
    t       = now_ist()
    weekday = t.weekday()
    month   = t.month

    if weekday not in TRADE_DAYS:
        pinfo(f"Today is {DAY_NAMES[weekday]} — not in TRADE_DAYS. Skipping.")
        return False
    if month in SKIP_MONTHS:
        pinfo(f"{MONTH_NAMES[month]} is in SKIP_MONTHS — skipping.")
        telegram(f"Skipping — {MONTH_NAMES[month]} is a configured skip month")
        return False

    pinfo(f"Trade day OK: {DAY_NAMES[weekday]}, {MONTH_NAMES[month]} {t.year}")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY — Short ATM Straddle via OptionsMultiOrder
#
#  underlying=NIFTY + exchange=NSE_INDEX  → OpenAlgo resolves ATM strike
#  Resolved option symbols returned (e.g. NIFTY27MAR2523000CE) are in NFO format
#  → Used for quotes() and orderstatus() calls
# ═══════════════════════════════════════════════════════════════════════════════

def place_entry() -> bool:
    """
    Place both legs atomically. Returns True on full success, False otherwise.
    On partial fill → emergency close triggered immediately.
    State is saved to disk after successful entry.
    """
    expiry = get_expiry()
    psep()
    pinfo("PLACING ENTRY ORDERS")
    pinfo(f"  Underlying: {UNDERLYING}  Exchange: {EXCHANGE}  Expiry: {expiry}")
    pinfo(f"  Offset: {STRIKE_OFFSET}  Qty/leg: {qty()}  Product: {PRODUCT}")
    psep()

    try:
        resp = client.optionsmultiorder(
            strategy   = STRATEGY_NAME,
            underlying = UNDERLYING,
            exchange   = EXCHANGE,      # NSE_INDEX — OpenAlgo resolves ATM from live price
            legs       = [
                {
                    "offset"      : STRIKE_OFFSET,
                    "option_type" : "CE",
                    "action"      : "SELL",
                    "quantity"    : qty(),
                    "expiry_date" : expiry,
                    "product"     : PRODUCT,
                    "pricetype"   : "MARKET",
                    "splitsize"   : 0
                },
                {
                    "offset"      : STRIKE_OFFSET,
                    "option_type" : "PE",
                    "action"      : "SELL",
                    "quantity"    : qty(),
                    "expiry_date" : expiry,
                    "product"     : PRODUCT,
                    "pricetype"   : "MARKET",
                    "splitsize"   : 0
                }
            ]
        )
    except Exception as e:
        perr(f"OptionsMultiOrder exception: {e}")
        telegram(f"ENTRY EXCEPTION\n{e}")
        return False

    if not isinstance(resp, dict) or resp.get("status") != "success":
        err = resp.get("message", str(resp)) if isinstance(resp, dict) else str(resp)
        perr(f"Entry FAILED: {err}")
        telegram(f"ENTRY FAILED\n{err}")
        return False

    results     = resp.get("results", [])
    filled_legs = {}

    for leg in results:
        opt_type = leg.get("option_type", "")
        if leg.get("status") == "success":
            filled_legs[opt_type] = {
                "symbol"  : leg.get("symbol", ""),
                "orderid" : leg.get("orderid", ""),
                "mode"    : leg.get("mode", "live"),
            }
            pinfo(
                f"  LEG {opt_type} OK | {leg.get('symbol')} | "
                f"OrderID: {leg.get('orderid')} | Mode: {leg.get('mode','live').upper()}"
            )
        else:
            perr(f"  LEG {opt_type} FAILED: {leg.get('message', 'Unknown')}")

    # Partial fill → emergency close
    if "CE" not in filled_legs or "PE" not in filled_legs:
        perr("PARTIAL ENTRY — emergency close triggered")
        telegram("PARTIAL ENTRY — emergency close triggered")
        close_all(reason="Emergency: Partial Entry Fill")
        return False

    # Update state
    today_ist = now_ist()
    state["in_position"]    = True
    state["symbol_ce"]      = filled_legs["CE"]["symbol"]
    state["symbol_pe"]      = filled_legs["PE"]["symbol"]
    state["orderid_ce"]     = filled_legs["CE"]["orderid"]
    state["orderid_pe"]     = filled_legs["PE"]["orderid"]
    state["underlying_ltp"] = float(resp.get("underlying_ltp", 0))
    state["entry_time"]     = today_ist.isoformat()
    state["entry_date"]     = today_ist.strftime("%Y-%m-%d")   # key for stale-state detection
    state["today_pnl"]      = 0.0
    state["exit_reason"]    = ""

    # Fetch actual fill prices
    capture_fill_prices()

    # ── PERSIST STATE TO DISK ─────────────────────────────────────────────────
    save_state()

    trade_mode = (results[0].get("mode", "live") if results else "live").upper()
    pinfo("ENTRY COMPLETE")
    pinfo(f"  NIFTY: {state['underlying_ltp']}  VIX: {state['vix_at_entry']}  Mode: {trade_mode}")
    pinfo(f"  CE: {state['symbol_ce']}  Fill: Rs.{state['entry_price_ce']:.2f}")
    pinfo(f"  PE: {state['symbol_pe']}  Fill: Rs.{state['entry_price_pe']:.2f}")
    pinfo(f"  State persisted to {STATE_FILE}")
    psep()

    telegram(
        f"ENTRY PLACED [{trade_mode}]\n"
        f"NIFTY: {state['underlying_ltp']}  VIX: {state['vix_at_entry']}\n"
        f"CE: {state['symbol_ce']} @ Rs.{state['entry_price_ce']:.2f}\n"
        f"PE: {state['symbol_pe']} @ Rs.{state['entry_price_pe']:.2f}\n"
        f"Expiry: {expiry}  Qty/leg: {qty()}"
    )
    return True


def capture_fill_prices():
    """
    Fetch average fill prices via OrderStatus for both legs.
    Required for accurate per-leg SL threshold calculation.
    """
    for leg, oid in [("CE", state["orderid_ce"]), ("PE", state["orderid_pe"])]:
        try:
            resp = client.orderstatus(order_id=oid, strategy=STRATEGY_NAME)
            if isinstance(resp, dict) and resp.get("status") == "success":
                avg_px = float(resp.get("data", {}).get("average_price", 0))
                state[f"entry_price_{leg.lower()}"] = avg_px
                pinfo(f"  Fill [{leg}]: Rs.{avg_px:.2f}  (OrderID: {oid})")
            else:
                pwarn(f"  OrderStatus failed for {leg} — SL using 0 as baseline")
        except Exception as e:
            pwarn(f"  Fill price fetch exception [{leg}]: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
#  EXIT — ClosePosition
# ═══════════════════════════════════════════════════════════════════════════════

def close_all(reason: str = "Scheduled Exit"):
    """
    Square off all positions for this strategy.
    Resets and persists state on success.
    On failure: logs error + Telegram alert — manual action required.
    """
    if not state["in_position"]:
        pinfo("close_all() — no open position, nothing to do")
        return

    psep()
    pinfo(f"CLOSING ALL POSITIONS  |  Reason: {reason}")

    try:
        resp = client.closeposition(strategy=STRATEGY_NAME)
    except Exception as e:
        perr(f"ClosePosition exception: {e}")
        perr("*** MANUAL ACTION REQUIRED — close positions in broker terminal ***")
        telegram(
            f"EXIT FAILED (exception)\nMANUAL ACTION REQUIRED\n"
            f"CE: {state['symbol_ce']}\nPE: {state['symbol_pe']}\nError: {e}"
        )
        return

    if isinstance(resp, dict) and resp.get("status") == "success":
        duration_str = ""
        if state.get("entry_time"):
            try:
                entry_dt = datetime.fromisoformat(state["entry_time"]).replace(tzinfo=IST)
                mins     = int((now_ist() - entry_dt).total_seconds() // 60)
                duration_str = f"  |  Held: {mins} min"
            except Exception:
                pass

        final_pnl           = state["today_pnl"]
        state["in_position"]    = False
        state["symbol_ce"]      = ""
        state["symbol_pe"]      = ""
        state["orderid_ce"]     = ""
        state["orderid_pe"]     = ""
        state["entry_price_ce"] = 0.0
        state["entry_price_pe"] = 0.0
        state["underlying_ltp"] = 0.0
        state["vix_at_entry"]   = 0.0
        state["entry_time"]     = None
        state["entry_date"]     = None
        state["exit_reason"]    = reason
        state["trade_count"]   += 1

        # ── PERSIST CLEARED STATE, THEN REMOVE FILE ───────────────────────────
        clear_state_file()

        pinfo(f"EXIT COMPLETE  |  Reason: {reason}  |  Approx P&L: Rs.{final_pnl:.0f}{duration_str}")
        pinfo(f"Session trade count: {state['trade_count']}")
        psep()

        telegram(
            f"EXIT COMPLETE\n"
            f"Reason: {reason}\n"
            f"Approx P&L: Rs.{final_pnl:.0f}{duration_str}\n"
            f"Session trades: {state['trade_count']}"
        )

    else:
        err_msg = resp.get("message", str(resp)) if isinstance(resp, dict) else str(resp)
        perr(f"ClosePosition FAILED: {err_msg}")
        perr("*** MANUAL ACTION REQUIRED — close positions in broker terminal ***")
        telegram(
            f"EXIT FAILED — MANUAL ACTION REQUIRED\n"
            f"Strategy: {STRATEGY_NAME}\n"
            f"CE: {state['symbol_ce']}\nPE: {state['symbol_pe']}\nError: {err_msg}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  P&L MONITOR
#  Quotes API: symbol=<NIFTY27MAR2523000CE>, exchange=NFO
#  NFO for F&O contracts (not NSE_INDEX — that is only for the underlying index)
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_live_pnl() -> float:
    """
    Fetch live LTP for each option leg and compute MTM P&L.

    Exchange for option quotes: NFO (confirmed from OpenAlgo docs)
      NSE_INDEX → only for underlying index (NIFTY spot, INDIAVIX)
      NFO       → all F&O option contracts (NIFTY27MAR2523000CE etc.)

    P&L formula (SELL legs):
      leg_pnl = (entry_price - current_ltp) * quantity
      +ve → LTP fell → profit for seller
      -ve → LTP rose → loss for seller

    Also enforces LEG_SL_PERCENT — triggers close_all() if breached.
    """
    if not state["in_position"]:
        return 0.0

    total_pnl = 0.0

    for leg, symbol, entry_px in [
        ("CE", state["symbol_ce"], state["entry_price_ce"]),
        ("PE", state["symbol_pe"], state["entry_price_pe"]),
    ]:
        if not symbol:
            continue

        try:
            q = client.quotes(symbol=symbol, exchange=OPTION_EXCH)  # exchange MUST be NFO
        except Exception as e:
            pwarn(f"Quotes exception for {symbol}: {e}")
            continue

        if not isinstance(q, dict) or q.get("status") != "success":
            pwarn(f"Quotes failed {symbol}: {q.get('message','') if isinstance(q,dict) else str(q)}")
            continue

        ltp = float(q.get("data", {}).get("ltp", 0))
        if ltp <= 0:
            pwarn(f"Invalid LTP {ltp} for {symbol}")
            continue

        if entry_px > 0:
            leg_pnl    = (entry_px - ltp) * qty()
            total_pnl += leg_pnl
            pdebug(f"  {leg} {symbol} | Entry Rs.{entry_px:.2f} | LTP Rs.{ltp:.2f} | P&L Rs.{leg_pnl:.0f}")

            # Per-leg SL check
            if LEG_SL_PERCENT > 0:
                sl_level = entry_px * (1.0 + LEG_SL_PERCENT / 100.0)
                if ltp >= sl_level:
                    pwarn(
                        f"LEG SL TRIGGERED: {leg} | {symbol} | "
                        f"Entry Rs.{entry_px:.2f} | LTP Rs.{ltp:.2f} | "
                        f"SL Rs.{sl_level:.2f} ({LEG_SL_PERCENT}%)"
                    )
                    telegram(
                        f"LEG SL HIT: {leg}\n{symbol}\n"
                        f"Entry Rs.{entry_px:.2f}  LTP Rs.{ltp:.2f}\n"
                        f"SL Rs.{sl_level:.2f} ({LEG_SL_PERCENT}%)\nClosing all."
                    )
                    close_all(reason=f"{leg} Leg SL {LEG_SL_PERCENT}% Triggered")
                    return total_pnl
        else:
            pinfo(f"  {leg} {symbol} | LTP Rs.{ltp:.2f}  (entry price not captured)")

    return total_pnl


def monitor_pnl():
    if not state["in_position"]:
        return

    pnl = fetch_live_pnl()
    state["today_pnl"] = pnl

    pinfo(
        f"MONITOR | MTM P&L: Rs.{pnl:.0f} | "
        f"Target: Rs.{DAILY_PROFIT_TARGET} | "
        f"Limit: Rs.{DAILY_LOSS_LIMIT}"
    )

    if DAILY_PROFIT_TARGET > 0 and pnl >= DAILY_PROFIT_TARGET:
        pinfo(f"DAILY PROFIT TARGET Rs.{DAILY_PROFIT_TARGET} REACHED — exiting")
        close_all(reason=f"Daily Profit Target Rs.{DAILY_PROFIT_TARGET}")
        return

    if DAILY_LOSS_LIMIT < 0 and pnl <= DAILY_LOSS_LIMIT:
        pwarn(f"DAILY LOSS LIMIT Rs.{DAILY_LOSS_LIMIT} BREACHED — exiting")
        close_all(reason=f"Daily Loss Limit Rs.{DAILY_LOSS_LIMIT}")
        return


# ═══════════════════════════════════════════════════════════════════════════════
#  SCHEDULED JOBS
# ═══════════════════════════════════════════════════════════════════════════════

def job_entry():
    psep()
    pinfo(f"ENTRY JOB | {now_ist().strftime('%A %d-%b-%Y %H:%M:%S IST')}")
    psep()

    if state["in_position"]:
        pwarn("Already in position — entry skipped (duplicate guard)")
        return

    if not trade_day_ok():
        return
    if not vix_ok():
        return

    state["today_pnl"] = 0.0
    success = place_entry()
    if not success:
        perr("Entry failed — no position opened today")
        telegram("Entry FAILED — no position opened. Check logs.")


def job_exit():
    psep()
    pinfo(f"EXIT JOB | {now_ist().strftime('%A %d-%b-%Y %H:%M:%S IST')}")
    psep()

    if not state["in_position"]:
        pinfo("No open position at scheduled exit — nothing to do")
        return

    close_all(reason=f"Scheduled Exit at {EXIT_TIME}")


def job_monitor():
    if state["in_position"]:
        monitor_pnl()


# ═══════════════════════════════════════════════════════════════════════════════
#  MANUAL CONTROLS
# ═══════════════════════════════════════════════════════════════════════════════

def manual_entry():
    """Force entry immediately — bypasses time check, runs all other filters."""
    pinfo("MANUAL ENTRY triggered")
    job_entry()

def manual_exit():
    """Force close all positions immediately."""
    pinfo("MANUAL EXIT triggered")
    close_all(reason="Manual Exit by Operator")

def show_state():
    """Print full state to stdout."""
    psep()
    pinfo("STRATEGY STATE DUMP:")
    for k, v in state.items():
        print(f"    {k:<20} : {v}", flush=True)
    psep()

def check_connection():
    """Test OpenAlgo connection and display funds."""
    psep()
    pinfo("Testing OpenAlgo connection...")
    try:
        resp = client.funds()
        if isinstance(resp, dict) and resp.get("status") == "success":
            data = resp.get("data", {})
            pinfo(f"Connection : OK")
            pinfo(f"Available  : Rs.{data.get('availablecash', 'N/A')}")
            pinfo(f"Utilized   : Rs.{data.get('utiliseddebits', 'N/A')}")
            pinfo(f"M2M Unreal : Rs.{data.get('m2munrealized', 'N/A')}")
        else:
            perr(f"Connection FAILED: {resp}")
    except Exception as e:
        perr(f"Connection exception: {e}")
    psep()


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN SCHEDULER
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    """
    Start production strategy:
    1. Print banner
    2. Check OpenAlgo connection
    3. RECONCILE STATE with live broker positions (restart-safe)
    4. Start scheduler: entry / exit / monitor jobs
    5. On Ctrl+C or crash → emergency close if needed
    """
    print_banner()
    check_connection()

    # ── STARTUP RECONCILIATION — CRITICAL for restart safety ──────────────────
    reconcile_on_startup()

    entry_h, entry_m = parse_hhmm(ENTRY_TIME)
    exit_h,  exit_m  = parse_hhmm(EXIT_TIME)

    scheduler = BlockingScheduler(timezone=IST)

    scheduler.add_job(
        func=job_entry, trigger="cron", day_of_week="mon-fri",
        hour=entry_h, minute=entry_m,
        id="entry_job", name=f"Entry {ENTRY_TIME}",
        misfire_grace_time=60
    )
    scheduler.add_job(
        func=job_exit, trigger="cron", day_of_week="mon-fri",
        hour=exit_h, minute=exit_m,
        id="exit_job", name=f"Exit {EXIT_TIME}",
        misfire_grace_time=120
    )
    scheduler.add_job(
        func=job_monitor, trigger="interval", seconds=MONITOR_INTERVAL_S,
        id="monitor_job", name=f"Monitor {MONITOR_INTERVAL_S}s"
    )

    pinfo(f"Scheduler started | Entry: {ENTRY_TIME} | Exit: {EXIT_TIME} | Monitor: {MONITOR_INTERVAL_S}s")
    pinfo("Press Ctrl+C to stop gracefully")
    print("", flush=True)

    telegram(
        f"Strategy STARTED v{VERSION}\n"
        f"Entry: {ENTRY_TIME}  Exit: {EXIT_TIME}\n"
        f"Lots: {NUMBER_OF_LOTS}x{LOT_SIZE}={qty()} qty/leg\n"
        f"VIX: {VIX_MIN}-{VIX_MAX}  SL: {LEG_SL_PERCENT}%\n"
        f"Days: {', '.join(DAY_NAMES[d][:3] for d in sorted(TRADE_DAYS))}"
    )

    try:
        scheduler.start()

    except (KeyboardInterrupt, SystemExit):
        pinfo("Strategy stopped by operator (Ctrl+C)")
        if state["in_position"]:
            pwarn("Open position on shutdown — closing for safety")
            close_all(reason="Emergency: Script Stopped by Operator")
        telegram("Strategy STOPPED by operator")

    except Exception as e:
        perr(f"Scheduler crashed: {e}")
        if state["in_position"]:
            perr("Attempting emergency close after crash")
            close_all(reason="Emergency: Scheduler Crash")
        telegram(f"Strategy CRASHED\n{e}\nCheck logs immediately.")
        raise


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Default: run() — full production scheduler with restart reconciliation.

    For testing (comment run() and uncomment one below):
      check_connection()  → verify OpenAlgo reachable + show funds
      manual_entry()      → place entry immediately (skips time filter)
      manual_exit()       → close all positions immediately
      show_state()        → print current in-memory + file state
    """

    # Production
    run()

    # Testing
    # check_connection()
    # manual_entry()
    # manual_exit()
    # show_state()